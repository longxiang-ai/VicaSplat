from math import isqrt
from typing import Literal, Optional

import torch
from diff_gaussian_rasterization import (
    GaussianRasterizationSettings,
    GaussianRasterizer,
)
from einops import einsum, rearrange, repeat
from jaxtyping import Float
from torch import Tensor

from ...geometry.projection import get_fov, homogenize_points

from gsplat.rendering import rasterization


def get_projection_matrix(
    near: Float[Tensor, " batch"],
    far: Float[Tensor, " batch"],
    fov_x: Float[Tensor, " batch"],
    fov_y: Float[Tensor, " batch"],
) -> Float[Tensor, "batch 4 4"]:
    """Maps points in the viewing frustum to (-1, 1) on the X/Y axes and (0, 1) on the Z
    axis. Differs from the OpenGL version in that Z doesn't have range (-1, 1) after
    transformation and that Z is flipped.
    """
    tan_fov_x = (0.5 * fov_x).tan()
    tan_fov_y = (0.5 * fov_y).tan()

    top = tan_fov_y * near
    bottom = -top
    right = tan_fov_x * near
    left = -right

    (b,) = near.shape
    result = torch.zeros((b, 4, 4), dtype=torch.float32, device=near.device)
    result[:, 0, 0] = 2 * near / (right - left)
    result[:, 1, 1] = 2 * near / (top - bottom)
    result[:, 0, 2] = (right + left) / (right - left)
    result[:, 1, 2] = (top + bottom) / (top - bottom)
    result[:, 3, 2] = 1
    result[:, 2, 2] = far / (far - near)
    result[:, 2, 3] = -(far * near) / (far - near)
    return result

def render_gsplat(
    extrinsics: Float[Tensor, "batch view 4 4"],
    intrinsics: Float[Tensor, "batch view 3 3"],
    near: Float[Tensor, " batch view"],
    far: Float[Tensor, " batch view"],
    image_shape: tuple[int, int],
    background_color: Float[Tensor, "batch 3"],
    gaussian_means: Float[Tensor, "batch gaussian 3"],
    gaussian_covariances: Float[Tensor, "batch gaussian 3 3"],
    gaussian_sh_coefficients: Float[Tensor, "batch gaussian 3 d_sh"],
    gaussian_opacities: Float[Tensor, "batch gaussian"],
    scale_invariant: bool = True,
    use_sh: bool = True,
    sh_degree: Optional[int] = None,
    frame_by_frame: bool = False,
) -> tuple[Float[Tensor, "batch 3 height width"], Float[Tensor, "batch height width"]]:
    assert use_sh or gaussian_sh_coefficients.shape[-1] == 1

    # Make sure everything is in a range where numerical issues don't appear.
    if scale_invariant:
        scale = 1 / near
        extrinsics = extrinsics.clone()
        extrinsics[..., :3, 3] = extrinsics[..., :3, 3] * scale[:, None]
        gaussian_covariances = gaussian_covariances * (scale[:, None, None, None] ** 2)
        gaussian_means = gaussian_means * scale[:, None, None]
        near = near * scale
        far = far * scale

    _,  _, _, n = gaussian_sh_coefficients.shape
    degree = sh_degree or isqrt(n) - 1
    shs = rearrange(gaussian_sh_coefficients, "b g xyz n -> b g n xyz").contiguous()

    b, v = extrinsics.shape[:2]
    h, w = image_shape

    view_matrix = extrinsics.inverse()
    K = intrinsics.clone()
    K[..., 0, :] *= w
    K[..., 1, :] *= h

    all_images = []
    all_radii = []
    all_depths = []
    for i in range(b):
        if frame_by_frame:
            renders_ = []
            render_alphas_ = []
            for j in range(v):
                renders, render_alphas, meta = rasterization(
                    means=gaussian_means[i],
                    quats=None,
                    scales=None,
                    covars=gaussian_covariances[i],
                    opacities=gaussian_opacities[i],
                    colors=shs[i],
                    viewmats=view_matrix[i, j:j+1],
                    Ks=K[i, j:j+1],
                    width=w,
                    height=h,
                    packed=True,    # less memory, but slower
                    absgrad=False,
                    sparse_grad=False,
                    rasterize_mode='classic', # 'antialiased',
                    render_mode='RGB+D',
                    near_plane=near[i][0],
                    far_plane=far[i][0],
                    sh_degree=degree,
                )
                renders_.append(renders)
                render_alphas_.append(render_alphas)
            renders = torch.cat(renders_, dim=0)
            render_alphas = torch.cat(render_alphas_, dim=0)
        else:
            renders, render_alphas, meta = rasterization(
                means=gaussian_means[i],
                quats=None,
                scales=None,
                covars=gaussian_covariances[i],
                opacities=gaussian_opacities[i],
                colors=shs[i],
                viewmats=view_matrix[i],
                Ks=K[i],
                width=w,
                height=h,
                packed=True,    # less memory, but slower
                absgrad=False,
                sparse_grad=False,
                rasterize_mode='classic', # 'antialiased',
                render_mode='RGB+D',
                near_plane=near[i][0],
                far_plane=far[i][0],
                sh_degree=degree,
            )

        image, depth = renders[..., :3], renders[..., -1]

        all_images.append(image)
        all_depths.append(depth)
    return torch.cat(all_images, dim=0).permute(0, 3, 1, 2), torch.cat(all_depths, dim=0)


def render_cuda(
    extrinsics: Float[Tensor, "batch 4 4"],
    intrinsics: Float[Tensor, "batch 3 3"],
    near: Float[Tensor, " batch"],
    far: Float[Tensor, " batch"],
    image_shape: tuple[int, int],
    background_color: Float[Tensor, "batch 3"],
    gaussian_means: Float[Tensor, "batch gaussian 3"],
    gaussian_covariances: Float[Tensor, "batch gaussian 3 3"],
    gaussian_sh_coefficients: Float[Tensor, "batch gaussian 3 d_sh"],
    gaussian_opacities: Float[Tensor, "batch gaussian"],
    scale_invariant: bool = True,
    cam_rot_delta: Float[Tensor, "batch 3"] | None = None,
    cam_trans_delta: Float[Tensor, "batch 3"] | None = None,
    use_sh: bool = True,
    sh_degree: Optional[int] = None,
) -> tuple[Float[Tensor, "batch 3 height width"], Float[Tensor, "batch height width"]]:
    assert use_sh or gaussian_sh_coefficients.shape[-1] == 1
    shared_gaussians = gaussian_means.ndim == 2
    def get(gaussian_feature, idx):
        return gaussian_feature if shared_gaussians else gaussian_feature[idx]

    # # Make sure everything is in a range where numerical issues don't appear.
    # if scale_invariant:
    #     scale = 1 / near
    #     extrinsics = extrinsics.clone()
    #     extrinsics[..., :3, 3] = extrinsics[..., :3, 3] * scale[:, None]
    #     gaussian_covariances = gaussian_covariances * (scale[:, None, None, None] ** 2)
    #     gaussian_means = gaussian_means * scale[:, None, None]
    #     near = near * scale
    #     far = far * scale

    n = gaussian_sh_coefficients.shape[-1]
    degree = sh_degree or isqrt(n) - 1
    shs = rearrange(gaussian_sh_coefficients, "... xyz n -> ... n xyz").contiguous()

    b, _, _ = extrinsics.shape
    h, w = image_shape

    fov_x, fov_y = get_fov(intrinsics).unbind(dim=-1)
    tan_fov_x = (0.5 * fov_x).tan()
    tan_fov_y = (0.5 * fov_y).tan()

    projection_matrix = get_projection_matrix(near, far, fov_x, fov_y)
    projection_matrix = rearrange(projection_matrix, "b i j -> b j i")
    view_matrix = rearrange(extrinsics.inverse(), "b i j -> b j i")
    full_projection = view_matrix @ projection_matrix

    all_images = []
    all_radii = []
    all_depths = []
    for i in range(b):
        # Set up a tensor for the gradients of the screen-space means.
        mean_gradients = torch.zeros_like(gaussian_means[i], requires_grad=True)
        try:
            mean_gradients.retain_grad()
        except Exception:
            pass

        settings = GaussianRasterizationSettings(
            image_height=h,
            image_width=w,
            tanfovx=tan_fov_x[i].item(),
            tanfovy=tan_fov_y[i].item(),
            bg=background_color[i],
            scale_modifier=1.0,
            viewmatrix=view_matrix[i],
            projmatrix=full_projection[i],
            projmatrix_raw=projection_matrix[i],
            sh_degree=degree,
            campos=extrinsics[i, :3, 3],
            prefiltered=False,  # This matches the original usage.
            debug=False,
        )
        rasterizer = GaussianRasterizer(settings)

        row, col = torch.triu_indices(3, 3)

        image, radii, depth, opacity, n_touched = rasterizer(
            means3D=get(gaussian_means, i),
            means2D=mean_gradients,
            shs=get(shs, i) if use_sh else None,
            colors_precomp=None if use_sh else get(shs, i)[:, 0, :],
            opacities=get(gaussian_opacities, i)[..., None],
            cov3D_precomp=get(gaussian_covariances, i)[:, row, col],
            theta=cam_rot_delta[i] if cam_rot_delta is not None else None,
            rho=cam_trans_delta[i] if cam_trans_delta is not None else None,
        )
        all_images.append(image)
        all_radii.append(radii)
        all_depths.append(depth.squeeze(0))
    return torch.stack(all_images), torch.stack(all_depths)


def render_cuda_orthographic(
    extrinsics: Float[Tensor, "batch 4 4"],
    width: Float[Tensor, " batch"],
    height: Float[Tensor, " batch"],
    near: Float[Tensor, " batch"],
    far: Float[Tensor, " batch"],
    image_shape: tuple[int, int],
    background_color: Float[Tensor, "batch 3"],
    gaussian_means: Float[Tensor, "batch gaussian 3"],
    gaussian_covariances: Float[Tensor, "batch gaussian 3 3"],
    gaussian_sh_coefficients: Float[Tensor, "batch gaussian 3 d_sh"],
    gaussian_opacities: Float[Tensor, "batch gaussian"],
    fov_degrees: float = 0.1,
    use_sh: bool = True,
    dump: dict | None = None,
) -> Float[Tensor, "batch 3 height width"]:
    b, _, _ = extrinsics.shape
    h, w = image_shape
    assert use_sh or gaussian_sh_coefficients.shape[-1] == 1

    _, _, _, n = gaussian_sh_coefficients.shape
    degree = isqrt(n) - 1
    shs = rearrange(gaussian_sh_coefficients, "b g xyz n -> b g n xyz").contiguous()

    # Create fake "orthographic" projection by moving the camera back and picking a
    # small field of view.
    fov_x = torch.tensor(fov_degrees, device=extrinsics.device).deg2rad()
    tan_fov_x = (0.5 * fov_x).tan()
    distance_to_near = (0.5 * width) / tan_fov_x
    tan_fov_y = 0.5 * height / distance_to_near
    fov_y = (2 * tan_fov_y).atan()
    near = near + distance_to_near
    far = far + distance_to_near
    move_back = torch.eye(4, dtype=torch.float32, device=extrinsics.device)
    move_back[2, 3] = -distance_to_near
    extrinsics = extrinsics @ move_back

    # Escape hatch for visualization/figures.
    if dump is not None:
        dump["extrinsics"] = extrinsics
        dump["fov_x"] = fov_x
        dump["fov_y"] = fov_y
        dump["near"] = near
        dump["far"] = far

    projection_matrix = get_projection_matrix(
        near, far, repeat(fov_x, "-> b", b=b), fov_y
    )
    projection_matrix = rearrange(projection_matrix, "b i j -> b j i")
    view_matrix = rearrange(extrinsics.inverse(), "b i j -> b j i")
    full_projection = view_matrix @ projection_matrix

    all_images = []
    all_radii = []
    for i in range(b):
        # Set up a tensor for the gradients of the screen-space means.
        mean_gradients = torch.zeros_like(gaussian_means[i], requires_grad=True)
        try:
            mean_gradients.retain_grad()
        except Exception:
            pass

        settings = GaussianRasterizationSettings(
            image_height=h,
            image_width=w,
            tanfovx=tan_fov_x,
            tanfovy=tan_fov_y,
            bg=background_color[i],
            scale_modifier=1.0,
            viewmatrix=view_matrix[i],
            projmatrix=full_projection[i],
            projmatrix_raw=projection_matrix[i],
            sh_degree=degree,
            campos=extrinsics[i, :3, 3],
            prefiltered=False,  # This matches the original usage.
            debug=False,
        )
        rasterizer = GaussianRasterizer(settings)

        row, col = torch.triu_indices(3, 3)

        image, radii, depth, opacity, n_touched = rasterizer(
            means3D=gaussian_means[i],
            means2D=mean_gradients,
            shs=shs[i] if use_sh else None,
            colors_precomp=None if use_sh else shs[i, :, 0, :],
            opacities=gaussian_opacities[i, ..., None],
            cov3D_precomp=gaussian_covariances[i, :, row, col],
        )
        all_images.append(image)
        all_radii.append(radii)
    return torch.stack(all_images)


DepthRenderingMode = Literal["depth", "disparity", "relative_disparity", "log"]
