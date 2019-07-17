import warnings
import torch
import torch.nn as nn
import torch.distributions as dist
import torch.nn.functional as F

from .log_likelihood import log_zinb_positive, log_nb_positive
from .modules import DecoderSCVI, Encoder
from .iaf_encoder import EncoderIAF
from .utils import one_hot


class IAVAE(nn.Module):
    def __init__(
        self,
        n_input: int,
        n_batch: int = 0,
        n_labels: int = 0,
        n_hidden: int = 128,
        n_latent: int = 10,
        n_layers: int = 1,
        t: int = 3,
        dropout_rate: float = 5e-2,
        dispersion: str = "gene",
        log_variational: bool = True,
        reconstruction_loss: str = "zinb",
    ):
        """
        EXPERIMENTAL: Posterior functionalities may not be working

        Model does not implement Forward.
        Training should be performed with ratio_loss method

        :param n_input:
        :param n_batch:
        :param n_labels:
        :param n_hidden:
        :param n_latent:
        :param n_layers:
        :param t: Number of autoregressive steps
        :param dropout_rate:
        :param dispersion:
        :param log_variational:
        :param reconstruction_loss:
        """

        super().__init__()
        warnings.warn('EXPERIMENTAL: Posterior functionalities may not be working')
        self.dispersion = dispersion
        self.n_latent = n_latent
        self.log_variational = log_variational
        self.reconstruction_loss = reconstruction_loss
        # Automatically deactivate if useless
        self.n_batch = n_batch
        self.n_labels = n_labels

        if self.dispersion == "gene":
            self.px_r = torch.nn.Parameter(torch.randn(n_input))
        elif self.dispersion == "gene-batch":
            self.px_r = torch.nn.Parameter(torch.randn(n_input, n_batch))
        elif self.dispersion == "gene-label":
            self.px_r = torch.nn.Parameter(torch.randn(n_input, n_labels))
        else:  # gene-cell
            pass

        # latent space representation
        self.z_encoder = EncoderIAF(
            n_in=n_input,
            n_latent=n_latent,
            n_cat_list=None,
            n_layers=n_layers,
            t=t,
            n_hidden=n_hidden,
            dropout_rate=dropout_rate,
        )
        # l encoder goes from n_input-dimensional data to 1-d library size
        self.l_encoder = Encoder(
            n_input, 1, n_layers=1, n_hidden=n_hidden, dropout_rate=dropout_rate
        )
        # decoder goes from n_latent-dimensional space to n_input-d data
        self.decoder = DecoderSCVI(
            n_latent,
            n_input,
            n_cat_list=[n_batch],
            n_layers=n_layers,
            n_hidden=n_hidden,
        )

    def inference(self, x, batch_index=None, y=None, n_samples=1):
        """

        :param x:
        :param batch_index:
        :param y:
        :param n_samples:
        :return:
        """
        x_ = x
        if self.log_variational:
            x_ = torch.log(1 + x_)

        # Sampling
        z, _ = self.z_encoder(x_, y, n_samples)
        ql_m, ql_v, library = self.l_encoder(x_)

        if n_samples > 1:
            ql_m = ql_m.unsqueeze(0).expand((n_samples, ql_m.size(0), ql_m.size(1)))
            ql_v = ql_v.unsqueeze(0).expand((n_samples, ql_v.size(0), ql_v.size(1)))
            library = dist.Normal(ql_m, ql_v.sqrt()).sample()

        assert z.shape[0] == library.shape[0], 'Different n_samples'
        assert z.shape[1] == library.shape[1], 'Different n_batch'

        px_scale, px_r, px_rate, px_dropout = self.decoder(self.dispersion, z, library, batch_index, y)
        if self.dispersion == "gene-label":
            px_r = F.linear(one_hot(y, self.n_labels), self.px_r)  # px_r gets transposed - last dimension is nb genes
        elif self.dispersion == "gene-batch":
            px_r = F.linear(one_hot(batch_index, self.n_batch), self.px_r)
        elif self.dispersion == "gene":
            px_r = self.px_r
        px_r = torch.exp(px_r)
        return px_scale, px_r, px_rate, px_dropout, z, ql_m, ql_v, library

    def ratio_loss(self, x, local_l_mean, local_l_var, batch_index=None, y=None, return_mean=True):
        x_ = x
        if self.log_variational:
            x_ = torch.log(1 + x_)

        # variationnal probas computation
        z, log_qz_x = self.z_encoder(x, batch_index)
        ql_m, ql_v, library = self.l_encoder(x_)
        log_ql_x = dist.Normal(ql_m, torch.sqrt(ql_v)).log_prob(library).sum(dim=-1)

        # priors computation
        log_pz = dist.Normal(torch.zeros_like(z), torch.ones_like(z)).log_prob(z).sum(dim=-1)
        log_pl = dist.Normal(local_l_mean, torch.sqrt(local_l_var)).log_prob(library).sum(dim=-1)

        # reconstruction proba computation
        px_scale, px_r, px_rate, px_dropout = self.decoder(self.dispersion, z, library, batch_index, y)
        if self.dispersion == "gene-label":
            px_r = F.linear(one_hot(y, self.n_labels), self.px_r)  # px_r gets transposed - last dimension is nb genes
        elif self.dispersion == "gene-batch":
            px_r = F.linear(one_hot(batch_index, self.n_batch), self.px_r)
        elif self.dispersion == "gene":
            px_r = self.px_r
        px_r = torch.exp(px_r)

        log_px_zl = -self.get_reconstruction_loss(x, px_rate, px_r, px_dropout)

        ratio = (
            log_px_zl + log_pz + log_pl
            - log_qz_x - log_ql_x
        )
        if not return_mean:
            return ratio
        elbo = ratio.mean(dim=0)
        return -elbo

    def get_reconstruction_loss(self, x, px_rate, px_r, px_dropout):
        # Reconstruction Loss
        if self.reconstruction_loss == 'zinb':
            reconst_loss = -log_zinb_positive(x, px_rate, px_r, px_dropout)
        elif self.reconstruction_loss == 'nb':
            reconst_loss = -log_nb_positive(x, px_rate, px_r)
        else:
            raise NotImplementedError
        return reconst_loss

