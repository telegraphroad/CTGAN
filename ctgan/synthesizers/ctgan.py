import warnings
import copy
import numpy as np
import pandas as pd
import torch
from packaging import version
from torch import optim
from torch.nn import BatchNorm1d, Dropout, LeakyReLU, Linear, Module, ReLU, Sequential, functional

from ctgan.data_sampler import DataSampler
from ctgan.data_transformer import DataTransformer
from ctgan.synthesizers.base import BaseSynthesizer


class Discriminator(Module):

    def __init__(self, input_dim, discriminator_dim, pac=10):
        super(Discriminator, self).__init__()
        dim = input_dim * pac
        self.pac = pac
        self.pacdim = dim
        seq = []
        for item in list(discriminator_dim):
            seq += [Linear(dim, item), LeakyReLU(0.2), Dropout(0.5)]
            dim = item

        seq += [Linear(dim, 1)]
        self.seq = Sequential(*seq)

    def calc_gradient_penalty(self, real_data, fake_data, device='cpu', pac=10, lambda_=10):
        alpha = torch.rand(real_data.size(0) // pac, 1, 1, device=device)
        alpha = alpha.repeat(1, pac, real_data.size(1))
        alpha = alpha.view(-1, real_data.size(1))

        interpolates = alpha * real_data + ((1 - alpha) * fake_data)

        disc_interpolates = self(interpolates)

        gradients = torch.autograd.grad(
            outputs=disc_interpolates, inputs=interpolates,
            grad_outputs=torch.ones(disc_interpolates.size(), device=device),
            create_graph=True, retain_graph=True, only_inputs=True
        )[0]

        gradient_penalty = ((
            gradients.view(-1, pac * real_data.size(1)).norm(2, dim=1) - 1
        ) ** 2).mean() * lambda_

        return gradient_penalty

    def forward(self, input):
        assert input.size()[0] % self.pac == 0
        return self.seq(input.view(-1, self.pacdim))


class Residual(Module):

    def __init__(self, i, o):
        super(Residual, self).__init__()
        self.fc = Linear(i, o)
        self.bn = BatchNorm1d(o)
        self.relu = ReLU()

    def forward(self, input):
        out = self.fc(input)
        out = self.bn(out)
        out = self.relu(out)
        return torch.cat([out, input], dim=1)


class Generator(Module):

    def __init__(self, embedding_dim, generator_dim, data_dim):
        super(Generator, self).__init__()
        dim = embedding_dim
        seq = []
        for item in list(generator_dim):
            seq += [Residual(dim, item)]
            dim += item
        seq.append(Linear(dim, data_dim))
        self.seq = Sequential(*seq)
        self.dist_p1 = None
        self.dist_p2 = None
        self.dist_p3 = None

    def forward(self, input):
        data = self.seq(input)
        return data


class CTGANSynthesizer(BaseSynthesizer):
    """Conditional Table GAN Synthesizer.

    This is the core class of the CTGAN project, where the different components
    are orchestrated together.
    For more details about the process, please check the [Modeling Tabular data using
    Conditional GAN](https://arxiv.org/abs/1907.00503) paper.
    Args:
        embedding_dim (int):
            Size of the random sample passed to the Generator. Defaults to 128.
        generator_dim (tuple or list of ints):
            Size of the output samples for each one of the Residuals. A Residual Layer
            will be created for each one of the values provided. Defaults to (256, 256).
        discriminator_dim (tuple or list of ints):
            Size of the output samples for each one of the Discriminator Layers. A Linear Layer
            will be created for each one of the values provided. Defaults to (256, 256).
        generator_lr (float):
            Learning rate for the generator. Defaults to 2e-4.
        generator_decay (float):
            Generator weight decay for the Adam Optimizer. Defaults to 1e-6.
        discriminator_lr (float):
            Learning rate for the discriminator. Defaults to 2e-4.
        discriminator_decay (float):
            Discriminator weight decay for the Adam Optimizer. Defaults to 1e-6.
        batch_size (int):
            Number of data samples to process in each step.
        discriminator_steps (int):
            Number of discriminator updates to do for each generator update.
            From the WGAN paper: https://arxiv.org/abs/1701.07875. WGAN paper
            default is 5. Default used is 1 to match original CTGAN implementation.
        log_frequency (boolean):
            Whether to use log frequency of categorical levels in conditional
            sampling. Defaults to ``True``.
        verbose (boolean):
            Whether to have print statements for progress results. Defaults to ``False``.
        epochs (int):
            Number of training epochs. Defaults to 300.
        pac (int):
            Number of samples to group together when applying the discriminator.
            Defaults to 10.
        cuda (bool):
            Whether to attempt to use cuda for GPU computation.
            If this is False or CUDA is not available, CPU will be used.
            Defaults to ``True``.
        gen_prior (torch.distributions.Distribution):
            Generator prior
        variable_prior (bool):
            sample or rsample
        training_track (str):
            'GAN' or 'NF'
        nfloss (str):
            NF loss type: 'ML' or 'TA'
        nfgenerator
    """

    def __init__(self,gen_prior, embedding_dim=128, generator_dim=(256, 256), discriminator_dim=(256, 256),
                 generator_lr=2e-4, generator_decay=1e-6, discriminator_lr=2e-4,
                 discriminator_decay=1e-6, batch_size=500, discriminator_steps=1,
                 log_frequency=True, verbose=False, epochs=300, pac=10, cuda=True, training_track = 'GAN',nfgenerator = None,nfloss='ML',variable_prior=False,dist_p1=None,dist_p2=None,dist_p3=None):

        assert batch_size % 2 == 0

        self._embedding_dim = embedding_dim
        self.generator_dim = generator_dim
        self._discriminator_dim = discriminator_dim
        self.gen_prior = gen_prior
        self.generator_lr = generator_lr
        self.generator_decay = generator_decay
        self._discriminator_lr = discriminator_lr
        self._discriminator_decay = discriminator_decay
        self._training_track = training_track
        self._batch_size = batch_size
        self._discriminator_steps = discriminator_steps
        self._log_frequency = log_frequency
        self._verbose = verbose
        self._epochs = epochs
        self.nfgenerator = nfgenerator
        self.pac = pac
        self._variable_prior = variable_prior
        self._nfloss = nfloss
        self.glosses = []
        self.dlosses = []
        self.dist_p1 = dist_p1
        self.dist_p2 = dist_p2
        self.dist_p3 = dist_p3
        
        if not cuda or not torch.cuda.is_available():
            device = 'cpu'
        elif isinstance(cuda, str):
            device = cuda
        else:
            device = 'cuda'

        self._device = torch.device(device)

        self._transformer = None
        self._data_sampler = None
        self.generator = None

    @staticmethod
    def _gumbel_softmax(logits, tau=1, hard=False, eps=1e-10, dim=-1):
        """Deals with the instability of the gumbel_softmax for older versions of torch.

        For more details about the issue:
        https://drive.google.com/file/d/1AA5wPfZ1kquaRtVruCd6BiYZGcDeNxyP/view?usp=sharing
        Args:
            logits:
                […, num_features] unnormalized log probabilities
            tau:
                non-negative scalar temperature
            hard:
                if True, the returned samples will be discretized as one-hot vectors,
                but will be differentiated as if it is the soft sample in autograd
            dim (int):
                a dimension along which softmax will be computed. Default: -1.
        Returns:
            Sampled tensor of same shape as logits from the Gumbel-Softmax distribution.
        """
        if version.parse(torch.__version__) < version.parse("1.2.0"):
            for i in range(10):
                transformed = functional.gumbel_softmax(logits, tau=tau, hard=hard,
                                                        eps=eps, dim=dim)
                if not torch.isnan(transformed).any():
                    return transformed
            raise ValueError("gumbel_softmax returning NaN.")

        return functional.gumbel_softmax(logits, tau=tau, hard=hard, eps=eps, dim=dim)

    def _apply_activate(self, data):
        """Apply proper activation function to the output of the generator."""
        data_t = []
        st = 0
        for column_info in self._transformer.output_info_list:
            for span_info in column_info:
                if span_info.activation_fn == 'tanh':
                    ed = st + span_info.dim
                    data_t.append(torch.tanh(data[:, st:ed]))
                    st = ed
                elif span_info.activation_fn == 'softmax':
                    ed = st + span_info.dim
                    transformed = self._gumbel_softmax(data[:, st:ed], tau=0.2)
                    data_t.append(transformed)
                    st = ed
                else:
                    assert 0

        return torch.cat(data_t, dim=1)

    def _cond_loss(self, data, c, m):
        """Compute the cross entropy loss on the fixed discrete column."""
        loss = []
        st = 0
        st_c = 0
        for column_info in self._transformer.output_info_list:
            for span_info in column_info:
                if len(column_info) != 1 or span_info.activation_fn != "softmax":
                    # not discrete column
                    st += span_info.dim
                else:
                    ed = st + span_info.dim
                    ed_c = st_c + span_info.dim
                    tmp = functional.cross_entropy(
                        data[:, st:ed],
                        torch.argmax(c[:, st_c:ed_c], dim=1),
                        reduction='none'
                    )
                    loss.append(tmp)
                    st = ed
                    st_c = ed_c

        loss = torch.stack(loss, dim=1)

        return (loss * m).sum() / data.size()[0]

    def _validate_discrete_columns(self, train_data, discrete_columns):
        """Check whether ``discrete_columns`` exists in ``train_data``.

        Args:
            train_data (numpy.ndarray or pandas.DataFrame):
                Training Data. It must be a 2-dimensional numpy array or a pandas.DataFrame.
            discrete_columns (list-like):
                List of discrete columns to be used to generate the Conditional
                Vector. If ``train_data`` is a Numpy array, this list should
                contain the integer indices of the columns. Otherwise, if it is
                a ``pandas.DataFrame``, this list should contain the column names.
        """
        if isinstance(train_data, pd.DataFrame):
            invalid_columns = set(discrete_columns) - set(train_data.columns)
        elif isinstance(train_data, np.ndarray):
            invalid_columns = []
            for column in discrete_columns:
                if column < 0 or column >= train_data.shape[1]:
                    invalid_columns.append(column)
        else:
            raise TypeError('``train_data`` should be either pd.DataFrame or np.array.')

        if invalid_columns:
            raise ValueError('Invalid columns found: {}'.format(invalid_columns))

    def fit(self, train_data, discrete_columns=tuple(), epochs=None):
        """Fit the CTGAN Synthesizer models to the training data.

        Args:
            train_data (numpy.ndarray or pandas.DataFrame):
                Training Data. It must be a 2-dimensional numpy array or a pandas.DataFrame.
            discrete_columns (list-like):
                List of discrete columns to be used to generate the Conditional
                Vector. If ``train_data`` is a Numpy array, this list should
                contain the integer indices of the columns. Otherwise, if it is
                a ``pandas.DataFrame``, this list should contain the column names.
        """
        self._validate_discrete_columns(train_data, discrete_columns)

        if epochs is None:
            epochs = self._epochs
        else:
            warnings.warn(
                ('`epochs` argument in `fit` method has been deprecated and will be removed '
                 'in a future version. Please pass `epochs` to the constructor instead'),
                DeprecationWarning
            )
        self._data_loader = torch.utils.data.DataLoader(train_data.values,
                                batch_size=self._batch_size,shuffle=True,num_workers=0)

        ##self._transformer = DataTransformer()
        ##self._transformer.fit(train_data, discrete_columns)

        ##train_data = self._transformer.transform(train_data)

        # self._data_sampler = DataSampler(
        #     train_data,
        #     self._transformer.output_info_list,
        #     self._log_frequency)

        self.min_loss = 999999999999999999.99999999999999
        loss_d = 0.0
        data_dim = train_data.shape[1]
        self._data_dim = data_dim

        self.generator = Generator(
            self._embedding_dim,# + self._data_sampler.dim_cond_vec(),
            self.generator_dim,
            data_dim
        ).to(self._device)
        gparams = self.generator.parameters()
        
        if self._training_track == 'GAN':
            self.generator.dist_p1 = torch.nn.parameter.Parameter(self.dist_p1,requires_grad=True)
            self.generator.dist_p2 = torch.nn.parameter.Parameter(self.dist_p2,requires_grad=True)
            self.generator.dist_p3 = torch.nn.parameter.Parameter(self.dist_p3,requires_grad=True)
            if self.dist_p1 is not None:
                gparams = list(self.generator.parameters()) + list([self.generator.dist_p1,self.generator.dist_p2,self.generator.dist_p3])
                self.generator.register_parameter(name='TEST',param=torch.nn.Parameter(self.dist_p1))


        discriminator = Discriminator(
            data_dim,# + self._data_sampler.dim_cond_vec(),
            self._discriminator_dim,
            pac=self.pac
        ).to(self._device)

#         optimizerG = optim.AdamW(
#             self.generator.parameters(), lr=self.generator_lr, betas=(0.5, 0.9),
#             weight_decay=self.generator_decay
#         )
        optimizerG = optim.AdamW(
            gparams, lr=self.generator_lr, betas=(0.5, 0.9),
            weight_decay=self.generator_decay
        )

        if self._training_track == 'NF':
            self.nfgenerator.dist_p1 = torch.nn.parameter.Parameter(self.dist_p1,requires_grad=True)
            self.nfgenerator.dist_p2 = torch.nn.parameter.Parameter(self.dist_p2,requires_grad=True)
            self.nfgenerator.dist_p3 = torch.nn.parameter.Parameter(self.dist_p3,requires_grad=True)
            self.best_model_sd = self.nfgenerator.state_dict()

            nfoptimizer = torch.optim.AdamW(self.nfgenerator.parameters(),lr=1e-4)
        optimizerD = optim.AdamW(
            discriminator.parameters(), lr=self._discriminator_lr,
            betas=(0.5, 0.9), weight_decay=self._discriminator_decay
        )

        mean = torch.zeros(self._batch_size, self._embedding_dim, device=self._device)
        std = mean + 1

        steps_per_epoch = max(len(train_data) // self._batch_size, 1)
        
        #self.best_model = copy.copy(self)
        
        for i in range(epochs):
            for id_ in range(steps_per_epoch):
                if self._training_track == 'GAN':
                    for n in range(self._discriminator_steps):
                        if self._variable_prior:
                            #fakez = torch.FloatTensor(self.gen_prior.rsample([self._batch_size,self._embedding_dim]).cpu().numpy()).to(self._device)
                            fakez = self.gen_prior.rsample([self._batch_size,self._embedding_dim]).squeeze().to(self._device)
                            #print('fakezshape',fakez.shape)
                        else:
                            #fakez = torch.FloatTensor(self.gen_prior.sample([self._batch_size,self._embedding_dim]).cpu().numpy()).to(self._device)
                            fakez = self.gen_prior.sample([self._batch_size,self._embedding_dim]).to(self._device)

                        #condvec = self._data_sampler.sample_condvec(self._batch_size)
                        condvec = None
                        if condvec is None:
                            c1, m1, col, opt = None, None, None, None
                            real = next(iter(self._data_loader)).to(self._device)
                        else:
                            c1, m1, col, opt = condvec
                            c1 = torch.from_numpy(c1).to(self._device)
                            m1 = torch.from_numpy(m1).to(self._device)
                            fakez = torch.cat([fakez, c1], dim=1)

                            perm = np.arange(self._batch_size)
                            np.random.shuffle(perm)
                            real = self._data_sampler.sample_data(
                                self._batch_size, col[perm], opt[perm])
                            c2 = c1[perm]

                        
                        fake = self.generator(fakez)
                        #fakeact = self._apply_activate(fake)
                        fakeact = fake

                        #real = torch.from_numpy(real.astype('float32')).to(self._device)

                        if c1 is not None:
                            fake_cat = torch.cat([fakeact, c1], dim=1)
                            real_cat = torch.cat([real, c2], dim=1)
                        else:
                            real_cat = real.float()
                            fake_cat = fakeact.float()

                        y_fake = discriminator(fake_cat)
                        y_real = discriminator(real_cat)

                        pen = discriminator.calc_gradient_penalty(
                            real_cat, fake_cat, self._device, self.pac)
                        loss_d = -(torch.mean(y_real) - torch.mean(y_fake))
                        self.dlosses.append(loss_d.detach().cpu().numpy())

                        optimizerD.zero_grad()
                        pen.backward(retain_graph=True)
                        loss_d.backward()
                        optimizerD.step()

                    if self._variable_prior:
                        #fakez = torch.FloatTensor(self.gen_prior.rsample([self._batch_size,self._embedding_dim]).cpu().numpy()).to(self._device)
                        fakez = self.gen_prior.rsample([self._batch_size,self._embedding_dim]).squeeze().to(self._device)
                    else:
                        #fakez = torch.FloatTensor(self.gen_prior.sample([self._batch_size,self._embedding_dim]).cpu().numpy()).to(self._device)
                        fakez = self.gen_prior.sample([self._batch_size,self._embedding_dim]).to(self._device)
                    #condvec = self._data_sampler.sample_condvec(self._batch_size)
                    condvec = None

                    if condvec is None:
                        c1, m1, col, opt = None, None, None, None
                    else:
                        c1, m1, col, opt = condvec
                        c1 = torch.from_numpy(c1).to(self._device)
                        m1 = torch.from_numpy(m1).to(self._device)
                        fakez = torch.cat([fakez, c1], dim=1)

                    fake = self.generator(fakez)
                    #fakeact = self._apply_activate(fake)
                    fakeact = fake

                    if c1 is not None:
                        y_fake = discriminator(torch.cat([fakeact, c1], dim=1))
                    else:
                        y_fake = discriminator(fakeact)

                    if condvec is None:
                        cross_entropy = 0
                    else:
                        cross_entropy = self._cond_loss(fake, c1, m1)

                    loss_g = -torch.mean(y_fake) + cross_entropy
                    self.glosses.append(loss_g.detach().cpu().numpy())
                    if loss_g.item()<self.min_loss:
                        self.min_loss = loss_g.item()
                        #self.best_model = None
                        self.best_model_sd = self.generator.state_dict()
                        #print('new best performance detected!')

                    optimizerG.zero_grad()
                    loss_g.backward()
                    optimizerG.step()
                elif self._training_track == 'NF':
                    self.nfgenerator.train()
                    # fakez = self.nfgenerator.prior.sample((self._batch_size, data_dim)).to(self._device)

                    # fake,_,__ = self.nfgenerator(fakez)
                    # fakeact = fake      
                    #real = torch.from_numpy(real.detach().cpu().numpy().astype('float32')).to(self._device)
                    real = next(iter(self._data_loader)).float().to(self._device)
                    zs, prior_logprob, log_det = self.nfgenerator(real)
                    
                    #print(len(prior_logprob.shape))    
                    #print(prior_logprob.shape)
                    if len(prior_logprob.shape)>1:
                        prior_logprob = torch.mean(prior_logprob,axis=1)
                    logprob = prior_logprob + log_det
                    if self._nfloss == 'ML':
                        nfloss = -torch.mean(logprob)
                    elif self._nfloss == 'TA':
                        beta = -1
                        #s = self.nfgenerator.sample(2000)
                        s = real
                        logp = self.nfgenerator.prior.log_prob(s)
                        if len(logp.shape)>1:
                            logp = torch.mean(logp,axis=1)#mean!

                        logq = self.nfgenerator.log_prob(s)
                        diff = logp - logq
                        weights = torch.exp(diff - diff.max())
                        prob = torch.sign(weights.unsqueeze(1) - weights.unsqueeze(0))
                        prob = torch.greater(prob, 0.5).float()
                        F = 1 - prob.sum(1) / self._batch_size
                        gammas = F ** beta
                        gammas /= gammas.sum()
                        nfloss = -torch.sum(torch.unsqueeze(gammas * diff, 1))
                        #print(nfloss.item())
                    if nfloss.item() < self.min_loss:
                        self.min_loss = nfloss.item()
                        #self.best_model = None
                        self.best_model_sd = self.nfgenerator.state_dict()

                        #self.best_model = copy.deepcopy(self)
                        #print('new best performance detected!')

                    self.nfgenerator.zero_grad()
                    nfloss.backward(retain_graph=True)#!!!retain_graph=True
                    nfoptimizer.step()
                    self.glosses.append(nfloss.detach().cpu().numpy())

            if self._verbose:
                if self._training_track == 'GAN':
                    print(f"Epoch {i+1}, Loss G: {loss_g.detach().cpu(): .4f}, "
                      f"Loss D: {loss_d.detach().cpu(): .4f}",
                      flush=True)
                elif self._training_track == 'NF':
                    print(f"Epoch {i+1}, Loss G: {nfloss.detach().cpu(): .4f}, "
                      f"Loss D: {loss_d: .4f}",
                      flush=True)
        if self.nfgenerator is None:
            self.generator.load_state_dict(self.best_model_sd)
        else:
            self.nfgenerator.load_state_dict(self.best_model_sd)
    def sample(self, n, condition_column=None, condition_value=None):
        """Sample data similar to the training data.

        Choosing a condition_column and condition_value will increase the probability of the
        discrete condition_value happening in the condition_column.
        Args:
            n (int):
                Number of rows to sample.
            condition_column (string):
                Name of a discrete column.
            condition_value (string):
                Name of the category in the condition_column which we wish to increase the
                probability of happening.
        Returns:
            numpy.ndarray or pandas.DataFrame
        """
        if condition_column is not None and condition_value is not None:
            condition_info = self._transformer.convert_column_name_value_to_id(
                condition_column, condition_value)
            global_condition_vec = self._data_sampler.generate_cond_from_condition_column_info(
                condition_info, self._batch_size)
        else:
            global_condition_vec = None

        steps = n // self._batch_size + 1
        data = []
        if self._training_track == 'NF':
            self.nfgenerator.eval()
        for i in range(steps):
            if self._training_track == 'GAN':
                fakez = torch.FloatTensor(self.gen_prior.sample([self._batch_size,self._embedding_dim]).cpu().numpy()).to(self._device)
                #fakez = torch.FloatTensor(self.gen_prior.sample([self._batch_size,self._embedding_dim])).to(self._device)
                fake = self.generator(fakez)
            else:
                #fakez = self.nfgenerator.prior.sample((self._batch_size, self._data_dim)).to(self._device)
                fake = self.nfgenerator.sample(self._batch_size)


            #fakeact = self._apply_activate(fake)
            fakeact = fake
            data.append(fakeact.detach().cpu().numpy())

        data = np.concatenate(data, axis=0)
        data = data[:n]

        #return self._transformer.inverse_transform(data)
        return data

    def set_device(self, device):
        self._device = device
        if self.generator is not None:
            self.generator.to(self._device)
