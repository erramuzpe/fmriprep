#!/usr/bin/env python
# -*- coding: utf-8 -*-
# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
"""
Apply susceptibility distortion correction (SDC)

"""
from __future__ import print_function, division, absolute_import, unicode_literals

import pkg_resources as pkgr

from nipype.pipeline import engine as pe
from nipype.interfaces import utility as niu
from nipype.interfaces.ants import N4BiasFieldCorrection, Registration
from niworkflows.interfaces.registration import ANTSRegistrationRPT, ANTSApplyTransformsRPT

from fmriprep.interfaces.bids import ReadSidecarJSON, DerivativesDataSink
from fmriprep.interfaces.topup import ConformTopupInputs
SDC_UNWARP_NAME = 'SDC_unwarp'


def sdc_unwarp(name=SDC_UNWARP_NAME, ref_vol=None, method='jac', settings=None):
    """
    This workflow takes an estimated fieldmap and a target image and applies TOPUP,
    an :abbr:`SDC (susceptibility-derived distortion correction)` method in FSL to
    unwarp the target image.

    Input fields:
    ~~~~~~~~~~~~~

      inputnode.in_file - the 3D image to which this correction will be applied
      inputnode.in_mask - a brain mask corresponding to the in_file image
      inputnode.in_hmcpar - the head motion parameters as written by antsMotionCorr
      inputnode.fmap - a fieldmap in Hz
      inputnode.fmap_ref - the fieldmap reference (generally, a *magnitude* image or the
                           resulting SE image)
      inputnode.fmap_mask - a brain mask in fieldmap-space

    Output fields:
    ~~~~~~~~~~~~~~

      outputnode.out_file - the in_file after susceptibility-distortion correction.

    """

    workflow = pe.Workflow(name=name)
    inputnode = pe.Node(niu.IdentityInterface(
        fields=['in_file', 'in_hmcpar', 'in_mask', 'in_meta',
                'fmap_ref', 'fmap_mask', 'fmap']), name='inputnode')
    outputnode = pe.Node(niu.IdentityInterface(fields=['out_file', 'out_warped']), name='outputnode')

    # ref_inu = pe.Node(N4BiasFieldCorrection(dimension=3), name='reference_INU')
    # ref_bet = pe.Node(fsl.BET(frac=0.6, mask=True), name='reference_BET')
    ref_msk = pe.Node(niu.Function(input_names=['in_file', 'in_mask'],
                      output_names=['out_file'], function=_mask), name='reference_mask')
    target_msk = pe.Node(niu.Function(input_names=['in_file', 'in_mask'],
                         output_names=['out_file'], function=_mask), name='target_mask')

    # Fieldmap to rads
    torads = pe.Node(niu.Function(input_names=['in_file'], output_names=['out_file'],
                     function=hz2rads), name='fmap_Hz2rads')

    # Prepare reference image
    ref_wrp = pe.Node(niu.Function(
        function=_warp_reference,
        input_names=['fmap_ref', 'fmap', 'fmap_mask', 'metadata'],
        output_names=['fmap_ref', 'fmap_mask']),
        name='reference_warped')

    # Register the reference of the fieldmap to the reference
    # of the target image (the one that shall be corrected)
    ants_settings = pkgr.resource_filename('fmriprep', 'data/fmap-any_registration.json')
    if settings.get('debug', False):
        ants_settings = pkgr.resource_filename(
            'fmriprep', 'data/fmap-any_registration_testing.json')

    fmap2ref = pe.Node(Registration(
        from_file=ants_settings, output_warped_image='image2fmap.nii.gz',
        output_inverse_warped_image='fmap2image.nii.gz'), name='FMap2ImageMagnitude')

    # fugue = pe.Node(fsl.FUGUE(save_unmasked_fmap=True), name='fugue')
    applyxfm = pe.Node(ANTSApplyTransformsRPT(
        generate_report=False, dimension=3, interpolation='Linear'), name='FMap2ImageFieldmap')

    maskxfm = pe.Node(ANTSApplyTransformsRPT(
        generate_report=False, dimension=3, interpolation='NearestNeighbor'),
                      name='FMap2ImageMask')

    unwarp = pe.Node(niu.Function(
        input_names=['in_file', 'in_fieldmap', 'in_mask', 'metadata'],
        output_names=['out_file', 'out_report'], function=_fugue_unwarp),
                     name='Unwarping')

    dsunwarp = pe.Node(DerivativesDataSink(
        base_directory=settings['output_dir'], suffix='unwarp',
        out_path_base='reports'), name='DSunwarp')

    workflow.connect([
        (inputnode, torads, [(('fmap', _last), 'in_file')]),
        (inputnode, target_msk, [('in_file', 'in_file'),
                                 ('in_mask', 'in_mask')]),
        (target_msk, fmap2ref, [('out_file', 'moving_image')]),
        (inputnode, ref_wrp, [(('fmap_ref', _last), 'fmap_ref'),
                              (('fmap_mask', _last), 'fmap_mask'),
                              ('in_meta', 'metadata')]),
        (torads, ref_wrp, [('out_file', 'fmap')]),
        (ref_wrp, ref_msk, [('fmap_ref', 'in_file'),
                            ('fmap_mask', 'in_mask')]),
        (ref_msk, fmap2ref, [('out_file', 'fixed_image')]),
        (inputnode, applyxfm, [('in_file', 'reference_image')]),
        (fmap2ref, applyxfm, [
            ('reverse_transforms', 'transforms'),
            ('reverse_invert_flags', 'invert_transform_flags')]),
        (inputnode, maskxfm, [('in_file', 'reference_image')]),
        (fmap2ref, maskxfm, [
            ('reverse_transforms', 'transforms'),
            ('reverse_invert_flags', 'invert_transform_flags')]),
        (torads, applyxfm, [('out_file', 'input_image')]),
        (ref_wrp, maskxfm, [('fmap_mask', 'input_image')]),
        (inputnode, unwarp, [('in_file', 'in_file'),
                             ('in_meta', 'metadata')]),
        (applyxfm, unwarp, [('output_image', 'in_fieldmap')]),
        (maskxfm, unwarp, [('output_image', 'in_mask')]),
        (unwarp, outputnode, [('out_file', 'out_file')]),
        (inputnode, dsunwarp, [(('in_file', _first), 'source_file')]),
        (unwarp, dsunwarp, [('out_report', 'in_file')])
    ])

    # Disable ApplyTOPUP for now
    # encfile = pe.Node(interface=niu.Function(
    #     input_names=['input_images', 'in_dict'], output_names=['unwarp_param', 'warp_param'],
    #     function=create_encoding_file), name='TopUp_encfile', updatehash=True)
    # gen_movpar = pe.Node(GenerateMovParams(), name='GenerateMovPar')
    # topup_adapt = pe.Node(FieldCoefficients(), name='TopUpCoefficients')
    # # Use the least-squares method to correct the dropout of the input images
    # unwarp = pe.Node(fsl.ApplyTOPUP(method=method, in_index=[1]), name='TopUpApply')
    # workflow.connect([
    #     (inputnode, encfile, [('in_file', 'input_images')]),
    #     (meta, encfile, [('out_dict', 'in_dict')]),
    #     (conform, gen_movpar, [('out_file', 'in_file'),
    #                            ('out_movpar', 'in_mats')]),
    #     (conform, topup_adapt, [('out_brain', 'in_ref')]),
    #     #                       ('out_movpar', 'in_hmcpar')]),
    #     (gen_movpar, topup_adapt, [('out_movpar', 'in_hmcpar')]),
    #     (applyxfm, topup_adapt, [('output_image', 'in_file')]),
    #     (conform, unwarp, [('out_file', 'in_files')]),
    #     (topup_adapt, unwarp, [('out_fieldcoef', 'in_topup_fieldcoef'),
    #                            ('out_movpar', 'in_topup_movpar')]),
    #     (encfile, unwarp, [('unwarp_param', 'encoding_file')]),
    #     (unwarp, outputnode, [('out_corrected', 'out_file')])
    # ])

    return workflow

def _mask(in_file, in_mask, out_file=None):
    import nibabel as nb
    from fmriprep.utils.misc import genfname
    if out_file is None:
        out_file = genfname(in_file, 'brainmask')

    nii = nb.load(in_file)
    data = nii.get_data()
    data[nb.load(in_mask).get_data() <= 0] = 0
    nb.Nifti1Image(data, nii.affine, nii.header).to_filename(out_file)
    return out_file


def hz2rads(in_file, out_file=None):
    """Transform a fieldmap in Hz into rad/s"""
    from math import pi
    import nibabel as nb
    from fmriprep.utils.misc import genfname
    if out_file is None:
        out_file = genfname(in_file, 'rads')
    nii = nb.load(in_file)
    data = nii.get_data() * 2.0 * pi
    nb.Nifti1Image(data, nii.get_affine(),
                   nii.get_header()).to_filename(out_file)
    return out_file

def _fugue_unwarp(in_file, in_fieldmap, in_mask, metadata):
    import nibabel as nb
    from niworkflows.interfaces.registration import FUGUERPT
    from nipype.interfaces.fsl import FUGUE
    from fmriprep.utils.misc import genfname

    nii = nb.load(in_file)
    if nii.get_data().ndim == 4:
        nii_list = nb.four_to_three(nii)
    else:
        nii_list = [nii]

    if not isinstance(metadata, list):
        metadata = [metadata]

    if len(metadata) == 1:
        metadata = metadata * len(nii_list)

    out_report = None
    out_files = []
    for i, (tnii, tmeta) in enumerate(zip(nii_list, metadata)):
        tfile = genfname(in_file, 'vol%03d' % i)
        tnii.to_filename(tfile)
        ec = tmeta['EffectiveEchoSpacing']
        ud = tmeta['PhaseEncodingDirection'].replace('j', 'y').replace('i', 'x')

        fmap_umask = genfname(in_file, 'fmap_umask%03d' % i)
        FUGUE(
            fmap_in_file=in_fieldmap, save_unmasked_fmap=True,
            mask_file=in_mask, fmap_out_file=fmap_umask).run()

        vsm_file = genfname(in_file, 'vsm%03d' % i)
        FUGUE(
            fmap_in_file=fmap_umask, dwell_time=ec,
            unwarp_direction=ud, shift_out_file=vsm_file).run()

        gen_report = i == 0

        fugue = FUGUERPT(
            in_file=tfile, unwarp_direction=ud, icorr=True, shift_in_file=vsm_file,
            unwarped_file=genfname(in_file, 'unwarped%03d' % i),
            generate_report=gen_report)

        # print('Running FUGUE: %s' % fugue.cmdline)
        fugue_res = fugue.run()
        out_files.append(fugue_res.outputs.unwarped_file)

        if gen_report:
            out_report = fugue_res.outputs.out_report


    corr_nii = nb.concat_images([nb.load(f) for f in out_files])
    out_file = genfname(in_file, 'unwarped')
    corr_nii.to_filename(out_file)
    return out_file, out_report


def _warp_reference(fmap_ref, fmap, fmap_mask, metadata):
    import numpy as np
    import nibabel as nb
    from nipype.interfaces.fsl import FUGUE
    from fmriprep.utils.misc import genfname

    if isinstance(metadata, (list, tuple)):
        metadata = metadata[0]
    ec = metadata['EffectiveEchoSpacing']
    ud = metadata['PhaseEncodingDirection'].replace('j', 'y').replace('i', 'x')

    vsm_file = genfname(fmap_ref, 'vsm')
    gen_vsm = FUGUE(
        fmap_in_file=fmap, dwell_time=ec,
        unwarp_direction=ud, shift_out_file=vsm_file)
    gen_vsm.run()

    fugue = FUGUE(
        in_file=fmap_ref, shift_in_file=vsm_file, nokspace=True,
        forward_warping=True, unwarp_direction=ud, icorr=False,
        warped_file=genfname(fmap_ref, 'warped%s' % ud)).run()

    fuguemask = FUGUE(
        in_file=fmap_mask, shift_in_file=vsm_file, nokspace=True,
        forward_warping=True, unwarp_direction=ud, icorr=False,
        warped_file=genfname(fmap_mask, 'maskwarped%s' % ud)).run()

    masknii = nb.load(fuguemask.outputs.warped_file)
    mask = masknii.get_data()
    mask[mask > 0] = 1
    mask[mask < 1] = 0
    out_mask = genfname(fmap_mask, 'warp_bin')
    nb.Nifti1Image(mask.astype(np.uint8), masknii.affine,
                   masknii.header).to_filename(out_mask)
    return fugue.outputs.warped_file, out_mask

def _last(inlist):
    if isinstance(inlist, list):
        return inlist[-1]
    return inlist

def _first(inlist):
    if isinstance(inlist, list):
        return inlist[0]
    return inlist


def _fn_ras(in_file):
    import os.path as op
    import nibabel as nb
    from fmriprep.utils.misc import genfname

    if isinstance(in_file, list):
        in_file = in_file[0]

    out_file = genfname(in_file, suffix='ras')
    nb.as_closest_canonical(nb.load(in_file)).to_filename(
        out_file)
    return out_file


