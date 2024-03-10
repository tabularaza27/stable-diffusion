import torch
import functools
import torch.nn as nn
import torch.nn.functional as F

from taming.modules.losses.vqperceptual import *  # TODO: taming dependency yes/no?


class LPIPSWithDiscriminator(nn.Module):
    def __init__(self, disc_start, logvar_init=0.0, pixelloss_weight=1.0,
                 disc_num_layers=3, disc_in_channels=3, disc_factor=1.0, disc_weight=1.0,
                 perceptual_weight=1.0, use_actnorm=False, disc_conditional=False,
                 disc_loss="hinge", ndf=64, discriminator_3D=False, crop_to_profiles_2d=False, crop_mode="avg_dimensions"):

        super().__init__()
        assert disc_loss in ["hinge", "vanilla"]
        self.pixel_weight = pixelloss_weight
        # self.perceptual_loss = LPIPS().eval()
        self.perceptual_weight = perceptual_weight
        # output log variance
        self.logvar = nn.Parameter(torch.ones(size=()) * logvar_init)
        if discriminator_3D == True:
            # use 3D discriminator
            self.discriminator = NLayerDiscriminator3D(input_nc=disc_in_channels,
                                                    n_layers=disc_num_layers,
                                                    ndf=ndf
                                                    ).apply(weights_init)
            self.cond_concat_dim=-3

        else:
            # use 2D discriminator
            self.discriminator = NLayerDiscriminator(input_nc=disc_in_channels,
                                                    n_layers=disc_num_layers,
                                                    use_actnorm=use_actnorm,
                                                    ndf=ndf
                                                    ).apply(weights_init)
            self.cond_concat_dim=-2
            
        self.discriminator_iter_start = disc_start
        self.disc_loss = hinge_d_loss if disc_loss == "hinge" else vanilla_d_loss
        self.disc_factor = disc_factor
        self.discriminator_weight = disc_weight
        self.disc_conditional = disc_conditional
        self.crop_to_profiles_2d = crop_to_profiles_2d
        self.crop_mode = crop_mode

    def calculate_adaptive_weight(self, nll_loss, g_loss, last_layer=None):
        if last_layer is not None:
            nll_grads = torch.autograd.grad(nll_loss, last_layer, retain_graph=True)[0]
            g_grads = torch.autograd.grad(g_loss, last_layer, retain_graph=True)[0]
        else:
            nll_grads = torch.autograd.grad(nll_loss, self.last_layer[0], retain_graph=True)[0]
            g_grads = torch.autograd.grad(g_loss, self.last_layer[0], retain_graph=True)[0]

        d_weight = torch.norm(nll_grads) / (torch.norm(g_grads) + 1e-4)
        d_weight = torch.clamp(d_weight, 0.0, 1e4).detach()
        d_weight = d_weight * self.discriminator_weight
        return d_weight

    def forward(self, inputs, reconstructions, optimizer_idx,
                global_step, last_layer=None, cond=None, split="train",
                weights=None, overpass_mask=None):
        rec_loss = torch.abs(inputs.contiguous() - reconstructions.contiguous())
        #if self.perceptual_weight > 0:
        #    p_loss = self.perceptual_loss(inputs.contiguous(), reconstructions.contiguous())
        #    rec_loss = rec_loss + self.perceptual_weight * p_loss

        nll_loss = rec_loss / torch.exp(self.logvar) + self.logvar
        weighted_nll_loss = nll_loss
        if weights is not None:
            weighted_nll_loss = weights*nll_loss
        weighted_nll_loss = torch.sum(weighted_nll_loss) / weighted_nll_loss.shape[0]
        nll_loss = torch.sum(nll_loss) / nll_loss.shape[0]

        if self.crop_to_profiles_2d:
            reconstructions = LPIPSWithDiscriminator.get_2d_profiles(reconstructions,mode=self.crop_mode, overpass_mask=overpass_mask)
            inputs = LPIPSWithDiscriminator.get_2d_profiles(inputs,mode=self.crop_mode, overpass_mask=overpass_mask)
        
            if self.disc_conditional:
               assert self.disc_conditional
               # get overpass view of seviri
               cond = LPIPSWithDiscriminator.get_2d_profiles(cond,mode=self.crop_mode, overpass_mask=overpass_mask)
        

        # now the GAN part
        if optimizer_idx == 0:
            # generator update
            if cond is None:
                assert not self.disc_conditional
                logits_fake = self.discriminator(reconstructions.contiguous())
            else:
                assert self.disc_conditional
                logits_fake = self.discriminator(torch.cat((reconstructions.contiguous(), cond), dim=self.cond_concat_dim))
            g_loss = -torch.mean(logits_fake)

            if self.disc_factor > 0.0:
                try:
                    d_weight = self.calculate_adaptive_weight(nll_loss, g_loss, last_layer=last_layer)
                except RuntimeError:
                    assert not self.training
                    d_weight = torch.tensor(0.0)
            else:
                d_weight = torch.tensor(0.0)

            disc_factor = adopt_weight(self.disc_factor, global_step, threshold=self.discriminator_iter_start)
            loss = weighted_nll_loss + d_weight * disc_factor * g_loss

            log = {"{}/total_loss".format(split): loss.clone().detach().mean(), "{}/logvar".format(split): self.logvar.detach(),
                   "{}/rec_loss".format(split): rec_loss.detach().mean(),
                   "{}/d_weight".format(split): d_weight.detach(),
                   "{}/disc_factor".format(split): torch.tensor(disc_factor),
                   "{}/g_loss".format(split): g_loss.detach().mean(),
                   }
            return loss, log

        if optimizer_idx == 1:
            # second pass for discriminator update
            if cond is None:
                logits_real = self.discriminator(inputs.contiguous().detach())
                logits_fake = self.discriminator(reconstructions.contiguous().detach())
            else:
                logits_real = self.discriminator(torch.cat((inputs.contiguous().detach(), cond), dim=self.cond_concat_dim))
                logits_fake = self.discriminator(torch.cat((reconstructions.contiguous().detach(), cond), dim=self.cond_concat_dim))

            disc_factor = adopt_weight(self.disc_factor, global_step, threshold=self.discriminator_iter_start)
            d_loss = disc_factor * self.disc_loss(logits_real, logits_fake)

            log = {"{}/disc_loss".format(split): d_loss.clone().detach().mean(),
                   "{}/logits_real".format(split): logits_real.detach().mean(),
                   "{}/logits_fake".format(split): logits_fake.detach().mean()
                   }
            return d_loss, log

    @staticmethod
    def get_2d_profiles(cubes:torch.tensor, mode="avg_dimensions", overpass_mask=None):
        assert mode in ["avg_dimensions","padding"]
        
        if mode == "avg_dimensions":
            profiles_2d = torch.stack((cubes.mean(dim=-1), cubes.mean(dim=-2)),dim=1) # N x 2 x 256 x 64

        if mode == "padding":
            assert overpass_mask is not None, f"overpass mask has to be tensor not {overpass_mask}"
            profiles_2d = LPIPSWithDiscriminator._get_padded_profiles(cubes, overpass_mask) # N x 1 x 256 x 96

        return profiles_2d
    
    @staticmethod
    def _get_padded_profiles(cubes, overpass_mask, max_profile_length=96, pad_value=-1):
        """create profiles with equal length -> pad with pad_value to length 96

        Args:
            y_hat (_type_): N x Z x H x W 
            dardar (_type_): N x Z x H x W 
            overpass_mask (_type_): _description_

        Returns:
            torch.tensor, torch.tensor: 2d profiles of y_hat and dardar along over pass with dimensions N x 1 x Z x max_profile_length
        """
        batch_size = cubes.shape[0]
        profile_height = cubes.shape[1]

        profile_padded_list  = []

        for idx in range(batch_size):
            profile = torch.masked_select(cubes[idx], overpass_mask[idx].bool())
            profile = profile.reshape(profile_height, int(profile.shape[0]/profile_height))
            profile_padded = F.pad(profile, (0, max_profile_length-profile.shape[1]),value=pad_value)
            profile_padded = profile_padded.unsqueeze(0).unsqueeze(0)
            profile_padded_list.append(profile_padded)

        profile_padded = torch.concat(profile_padded_list,0)
        

        return profile_padded


# Defines the PatchGAN discriminator with the specified arguments.
# As seen here https://github.com/davidiommi/3D-CycleGan-Pytorch-MedImaging/blob/main/models/networks3D.py#L369
# only modified forward to add channel dimension
class NLayerDiscriminator3D(nn.Module):
    def __init__(self, input_nc, ndf=64, n_layers=3, norm_layer=nn.BatchNorm3d, use_sigmoid=False):
        super(NLayerDiscriminator3D, self).__init__()
        if type(norm_layer) == functools.partial:
            use_bias = norm_layer.func == nn.InstanceNorm3d
        else:
            use_bias = norm_layer == nn.InstanceNorm3d

        kw = 4
        padw = 1
        sequence = [
            nn.Conv3d(input_nc, ndf, kernel_size=kw, stride=2, padding=padw),
            nn.LeakyReLU(0.2, True)
        ]

        nf_mult = 1
        nf_mult_prev = 1
        for n in range(1, n_layers):
            nf_mult_prev = nf_mult
            nf_mult = min(2**n, 8)
            sequence += [
                nn.Conv3d(ndf * nf_mult_prev, ndf * nf_mult,
                          kernel_size=kw, stride=2, padding=padw, bias=use_bias),
                norm_layer(ndf * nf_mult),
                nn.LeakyReLU(0.2, True)
            ]

        nf_mult_prev = nf_mult
        nf_mult = min(2**n_layers, 8)
        sequence += [
            nn.Conv3d(ndf * nf_mult_prev, ndf * nf_mult,
                      kernel_size=kw, stride=1, padding=padw, bias=use_bias),
            norm_layer(ndf * nf_mult),
            nn.LeakyReLU(0.2, True)
        ]

        sequence += [nn.Conv3d(ndf * nf_mult, 1, kernel_size=kw, stride=1, padding=padw)]

        if use_sigmoid:
            sequence += [nn.Sigmoid()]

        self.model = nn.Sequential(*sequence)

    def forward(self, input):
        # modifiy input to add channel dimension
        return self.model(input)