"""
application: convert output of adjoint function to sensitivities to physical variables
like all transform in transfunc this is referenced from the transform function
eg: transform( sensitivity_instance, datadef.PhysicalAdjointData ) == condition_adjoint( sensitivity_instance )
"""

import numpy as np

import _get_root
from fourdvar.datadef import SensitivityData, PhysicalAdjointData
from fourdvar.util.date_handle import replace_date
import fourdvar.util.template_defn as template
import fourdvar.util.netcdf_handle as ncf
import fourdvar.util.global_config as global_config
import fourdvar.util.cmaq_config as cmaq_config

unit_key = 'units.<YYYYMMDD>'
unit_convert_dict = None

def get_unit_convert():
    """
    extension: get unit conversion dictionary for sensitivity to each days emissions
    input: None
    output: dict ('units.<YYYYMMDD>': np.ndarray( shape_of( template.sense_emis ) )
    
    notes: SensitivityData.emis units = CF/(ppm/s)
           PhysicalAdjointData.emis units = CF/(mol/(s*m^2))
    """
    global unit_key
    unit_dict = {}
    #all spcs have same shape, get from 1st
    tmp_spc = ncf.get_attr( template.sense_emis, 'VAR-LIST' ).split()[0]
    target_shape = ncf.get_variable( template.sense_emis, tmp_spc )[:].shape
    #layer thickness constant between files
    lay_sigma = list( ncf.get_attr( template.sense_emis, 'VGLVLS' ) )
    #layer thickness measured in scaled pressure units
    lay_thick = [ lay_sigma[ i ] - lay_sigma[ i+1 ] for i in range( len( lay_sigma ) - 1 ) ]
    lay_thick = np.array(lay_thick).reshape(( 1, len(lay_thick), 1, 1 ))
    
    const = global_config.ppm_scale * global_config.kg_scale * global_config.mwair
    
    for date in global_config.get_datelist():
        met_file = replace_date( cmaq_config.met_cro_3d, date )
        #slice off any extra layers above area of interest
        rhoj = ncf.get_variable( met_file, 'DENSA_J' )[ :, :len( lay_thick ), ... ]
        #assert timesteps are compatible
        assert (target_shape[0]-1) >= (rhoj.shape[0]-1), 'incompatible timesteps'
        assert (target_shape[0]-1) % (rhoj.shape[0]-1) == 0, 'incompatible timesteps'
        reps = (target_shape[0]-1) // (rhoj.shape[0]-1)
        
        rhoj_mapped = rhoj[ :-1, ... ].repeat( reps, axis=0 )
        rhoj_mapped = np.append( rhoj_mapped, rhoj[ -1:, ... ], axis=0 )
        unit_array = const / ( rhoj_mapped * lay_thick )
        
        day_label = replace_date( unit_key, date )
        unit_dict[ day_label ] = unit_array
    return unit_dict

def map_sense( sensitivity ):
    """
    application: map adjoint sensitivities to physical grid of unknowns.
    input: SensitivityData
    output: PhysicalAdjointData
    """
    global unit_convert_dict
    global unit_key
    if unit_convert_dict is None:
        unit_convert_dict = get_unit_convert()
    
    #check that:
    #- global_config dates exist
    #- PhysicalAdjointData params exist
    #- template.emis & template.sense_emis are compatible
    #- template.icon & template.sense_conc are compatible
    datelist = global_config.get_datelist()
    PhysicalAdjointData.assert_params()
    #all spcs use same dimension set, therefore only need to test 1.
    test_spc = PhysicalAdjointData.spcs[0]
    mod_shape = ncf.get_variable( template.emis, test_spc ).shape    
    
    #phys_params = ['tsec','nstep','nlays_icon','nlays_emis','nrows','ncols','spcs']
    #icon_dict = { spcs: np.ndarray( nlays_icon, nrows, ncols ) }
    #emis_dict = { spcs: np.ndarray( nstep, nlays_emis, nrows, ncols ) }
    
    #create blank constructors for PhysicalAdjointData
    p = PhysicalAdjointData
    icon_shape = ( p.nlays_icon, p.nrows, p.ncols, )
    emis_shape = ( p.nstep, p.nlays_emis, p.nrows, p.ncols, )
    icon_dict = { spc: np.zeros( icon_shape ) for spc in p.spcs }
    emis_dict = { spc: np.zeros( emis_shape ) for spc in p.spcs }
    del p
    
    #construct icon_dict
    icon_label = replace_date( 'conc.<YYYYMMDD>', datelist[0] )
    icon_fname = sensitivity.file_data[ icon_label ][ 'actual' ]
    icon_vars = ncf.get_variable( icon_fname, icon_dict.keys() )
    for spc in PhysicalAdjointData.spcs:
        data = icon_vars[ spc ][ 0, :, :, : ]
        ilays, irows, icols = data.shape
        msg = 'conc_sense and PhysicalAdjointData.{} are incompatible'
        assert ilays >= PhysicalAdjointData.nlays_icon, msg.format( 'nlays_icon' )
        assert irows == PhysicalAdjointData.nrows, msg.format( 'nrows' )
        assert icols == PhysicalAdjointData.ncols, msg.format( 'ncols' )
        icon_dict[ spc ] = data[ 0:PhysicalAdjointData.nlays_icon, :, : ].copy()
    
    p_daysize = (24*60*60) / PhysicalAdjointData.tsec
    emis_pattern = 'emis.<YYYYMMDD>'
    for i,date in enumerate( datelist ):
        label = replace_date( emis_pattern, date )
        sense_fname = sensitivity.file_data[ label ][ 'actual' ]
        sense_data_dict = ncf.get_variable( sense_fname, PhysicalAdjointData.spcs )
        start = i * p_daysize
        end = (i+1) * p_daysize
        for spc in PhysicalAdjointData.spcs:
            unit_convert = unit_convert_dict[ replace_date( unit_key, date ) ]
            sdata = sense_data_dict[ spc ][:] * unit_convert
            sstep, slay, srow, scol = sdata.shape
            #recast to match mod_shape
            mstep, mlay, mrow, mcol = mod_shape
            msg = 'emis_sense and ModelInputData {} are incompatible.'
            assert ((sstep-1) >= (mstep-1)) and ((sstep-1) % (mstep-1) == 0), msg.format( 'TSTEP' )
            assert slay >= mlay, msg.format( 'NLAYS' )
            assert srow == mrow, msg.format( 'NROWS' )
            assert scol == mcol, msg.format( 'NCOLS' )
            fac = (sstep-1) // (mstep-1)
            tmp = np.array([ sdata[ i:-1:fac, 0:mlay, ... ]
                             for i in range( fac ) ]).mean( axis=0 )
            tmp = np.append( tmp, sdata[ -1:, 0:mlay, ... ], axis=0 )
            #adjoint prepare_model
            msg = 'ModelInputData and PhysicalAdjointData.{} are incompatible.'
            assert ((mstep-1) >= p_daysize) and ((mstep-1) % p_daysize == 0), msg.format('nstep')
            assert mlay >= PhysicalAdjointData.nlays_emis, msg.format( 'nlays_emis' )
            assert mrow == PhysicalAdjointData.nrows, msg.format( 'nrows' )
            assert mcol == PhysicalAdjointData.ncols, msg.format( 'ncols' )
            fac = (mstep-1) // p_daysize
            pdata = np.array([ tmp[ i:-1:fac, 0:PhysicalAdjointData.nlays_emis, ... ]
                               for i in range( fac ) ]).sum( axis=0 )
            pdata = np.append( pdata, tmp[ -1:, 0:PhysicalAdjointData.nlays_emis, ... ], axis=0 )
            emis_dict[ spc ][ start:end+1, ... ] = pdata.copy()
    
    return PhysicalAdjointData( icon_dict, emis_dict )
