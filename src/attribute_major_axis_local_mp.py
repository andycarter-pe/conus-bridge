# From the 'major-axis lines' determine the 'hull' polygon and corresponding
# classified surface DEM that coresonds to that line.  Cut a profile of the
# major axis line for the ground and bridge deck.
#
# Created by: Andy Carter, PE
# Created - 2022.11.07
# Last revised - 2023.09.05 - centering the mjr axis on the hull
#
# tx-bridge - eigth processing script
# Uses the 'pdal' conda environment

# ************************************************************
import argparse
import geopandas as gpd
import pandas as pd

from shapely import wkt
from shapely.wkt import loads
from shapely.geometry import LineString, Polygon
from shapely.ops import split

import rioxarray as rio
from rasterio.io import MemoryFile
from rioxarray.merge import merge_arrays
from rasterio.errors import RasterioIOError

import numpy as np
import math
from scipy.signal import savgol_filter

import os
import tqdm

import time
import datetime

import json
import configparser
import rtree

import urllib.request

import multiprocessing as mp
from multiprocessing import Pool

import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)


# tx-bridge attributes sub-processes
from assign_feature_id_to_mjr_axis_local import fn_assign_feature_id_to_mjr_axis
from conflate_nbi import fn_conflate_nbi
from compute_low_chord_attributes import fn_compute_low_chord_attributes
from add_hull_geometry import fn_add_hull_geometry
from add_hull_geometry_early import fn_add_hull_geometry_early #added 2023.09.05 - for mjr axis line centering
from fetch_hand_rating_curves_local import fn_fetch_hand_rating_curves
from plot_cross_sections_mp import fn_plot_cross_sections
from generate_kml import fn_generate_kml

# ************************************************************


# ````````````````````````````````````````
def fn_json_from_ini(str_ini_path):
    # Read the INI file
    config = configparser.ConfigParser()
    config.read(str_ini_path)
    
    # Convert to a dictionary
    config_dict = {section: dict(config[section]) for section in config.sections()}
    
    # Convert to JSON
    json_data = json.dumps(config_dict, indent=4)
    
    return(json_data)
# ````````````````````````````````````````


# ````````````````````````````````````````
def fn_filelist(source, tpl_extenstion):
    # walk a directory and get files with suffix
    # returns a list of file paths
    # args:
    #   source = path to walk
    #   tpl_extenstion = tuple of the extensions to find (Example: (.tig, .jpg))
    #   str_dem_path = path of the dem that needs to be converted
    matches = []
    for root, dirnames, filenames in os.walk(source):
        for filename in filenames:
            if filename.endswith(tpl_extenstion):
                matches.append(os.path.join(root, filename))
    return matches
# ````````````````````````````````````````


# ----------------------------------------------  
# Print iterations progress
def fn_print_progress_bar (iteration,
                           total,
                           prefix = '', suffix = '',
                           decimals = 0,
                           length = 100, fill = '█',
                           printEnd = "\r"):
    """
    from: https://stackoverflow.com/questions/3173320/text-progress-bar-in-the-console

    """
    percent = ("{0:." + str(decimals) + "f}").format(100 * (iteration / float(total)))
    filledLength = int(length * iteration // total)
    bar = fill * filledLength + '-' * (length - filledLength)
    print(f'\r{prefix} |{bar}| {percent}% {suffix}', end = printEnd)
    # Print New Line on Complete
    if iteration == total: 
        print()
# ----------------------------------------------  


# >>>>>>>>>>>>>>>>>>>>>>>>>>>
def fn_distance(x1,x2,y1,y2):
    sq1 = (x1-x2)*(x1-x2)
    sq2 = (y1-y2)*(y1-y2)
    return math.sqrt(sq1 + sq2)
# >>>>>>>>>>>>>>>>>>>>>>>>>>>


# ---------------------------------------------
def fn_gdf_point_on_line(flt_perct_on_line, gdf_line_input):
    
    # create Geodataframe for a point on each line
    gdf_pt = gdf_line_input.copy()

    # geopandas point on of mjr axis lines
    gdf_pt['geometry'] = gdf_line_input.geometry.interpolate(flt_perct_on_line, normalized = True)
    
    return(gdf_pt)
# ---------------------------------------------


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
def fn_get_smooth_deck_and_ground_profile(gdf):
    
    # keep only the records that don't have null values - for deck smoothing
    #gdf_other = gdf[~gdf.isna().any(1)]
    gdf_other = gdf.dropna()
    
    # ------------
    # smooth the deck profile
    x_deck = list(gdf_other['h_distance'])
    y_deck = list(gdf_other['elev_deck'])

    int_window = len(x_deck) // 4

    # if even add one for the savgol filter
    if (int_window % 2) == 0:
        int_window += 1

    try:
        w_deck = savgol_filter(y_deck, int_window, 2)
    except:
        w_deck = y_deck
    
    # ------------
    # get the ground profile
    x_ground = list(gdf['h_distance'])
    y_ground = list(gdf['elev_grnd'])

    try:
        w_ground = y_ground
    except:
        w_ground = y_ground
    # ------------
    
    # ------------
    if type(w_ground) != list:
        list_w = w_ground.tolist()
    else:
        list_w = w_ground
        
    # create a pandas dataframe of station and ground
    df_ground = pd.DataFrame(list(zip(x_ground, list_w)),columns =['sta', 'ground_elev'])

    # create a pandas dataframe of the station and deck
    df_deck = pd.DataFrame(list(zip(x_deck, w_deck)),columns =['sta', 'deck_elev'])

    df_xs = df_ground.merge(df_deck, on='sta', how='left')

    df_xs["max_elev_road_deck"] = df_xs[["ground_elev", "deck_elev"]].max(axis=1)
    
    # -----------------------
    # returns a pandas dataframe of station, smooth ground, smooth deck and smoothed max of max_elev_road_deck
    # 'sta' 'ground_elev' 'deck_elev' 'max_elev_road_deck'
    return(df_xs)
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~


# .............................................
def fn_get_ground_dem_from_usgs_service(shp_mjr_axis_ar_buffer_lambert, b_is_feet, crs_lines):
    
    # TODO - 2022.11.15 - Service will retun strange ground (0 elevations and nan??)
    
    int_resolution = 1 # requested resolution in lambert units - meters
    
    # the geometry from the requested polygon as wellKnownText
    boundary_geom_WKT = shp_mjr_axis_ar_buffer_lambert

    # the bounding box of the requested lambert polygon
    b = boundary_geom_WKT.bounds

    # convert the bounding coordinates to integers
    list_int_b = []
    for i in b:
        list_int_b.append(int(i//1))

    int_tile_x = list_int_b[2] - list_int_b[0]
    int_tile_y = list_int_b[3] - list_int_b[1]

    str_URL_header = r'https://elevation.nationalmap.gov/arcgis/services/3DEPElevation/ImageServer/WCSServer?'
    str_URL_query_1 = r'SERVICE=WCS&VERSION=1.0.0&REQUEST=GetCoverage&coverage=DEP3Elevation&CRS=EPSG:3857&FORMAT=GeoTiff'
    
    # TODO - this is an override to use local WCS with geoserver - 2022.12.29
    # note that the coverage is 'cog'
    # note that format = 'geotiff' - all lower case
    str_URL_header = r'http://localhost:8080/geoserver/tnris/wcs?'
    str_URL_query_1 = r'SERVICE=WCS&VERSION=1.0.0&REQUEST=GetCoverage&coverage=cog&CRS=EPSG:3857&FORMAT=geotiff'

    str_bbox = str(list_int_b[0]) + "," + str(list_int_b[1]) + "," + str(list_int_b[2]) + "," + str(list_int_b[3])

    str_URL_query_bbox = "&BBOX=" + str_bbox
    str_URL_query_dim = "&WIDTH=" + str(int_tile_x/int_resolution) + "&HEIGHT=" + str(int_tile_y/int_resolution)
    
    # note - local WCS requires integer
    str_URL_query_dim = "&WIDTH=" + str(int(int_tile_x/int_resolution)) + "&HEIGHT=" + str(int(int_tile_y/int_resolution))

    str_url = str_URL_header + str_URL_query_1 + str_URL_query_bbox + str_URL_query_dim

    # url request to get terrain
    http_response_raster = urllib.request.urlopen(str_url)

    # convert the response to bytes
    byte_response_raster = http_response_raster.read()

    with MemoryFile(byte_response_raster) as memfile:
        with memfile.open() as ground_terrain_src:

            # read the DEM as a "Rioxarray"
            ground_dem = rio.open_rasterio(ground_terrain_src).squeeze()

            # reproject the raster to the same projection as the road
            ground_dem_local_proj = ground_dem.rio.reproject(crs_lines, nodata = np.nan)

            if b_is_feet:
                # scale the raster from meters to feet
                ground_dem_local_proj = ground_dem_local_proj * 3.28084
                
    return(ground_dem_local_proj)
# .............................................


# .............................................
def fn_get_ground_dem_from_provided_cog(shp_mjr_axis_ar_buffer_lambert, b_is_feet, crs_line, str_cog_dem_path):
    
    # added MAC - 2023.07.24 - to allow for local COG file

    # the geometry from the requested polygon as wellKnownText
    boundary_geom_WKT = shp_mjr_axis_ar_buffer_lambert

    # the bounding box of the requested lambert polygon
    b = boundary_geom_WKT.bounds

    # create rio xarray of the clipped boundary
    xar_dem = rio.open_rasterio(
        filename=str_cog_dem_path,
        chunks='auto',
        parse_coordinates=True,
        masked=True).rio.clip_box(minx=b[0], miny=b[1], maxx=b[2], maxy=b[3])
    
    # read the DEM as a "Rioxarray"
    ground_dem = xar_dem.squeeze()

    # reproject the raster to the same projection as the road
    ground_dem_local_proj = ground_dem.rio.reproject(crs_line, nodata = np.nan)

    if b_is_feet:
        # scale the raster from meters to feet
        ground_dem_local_proj = ground_dem_local_proj * 3.28084
   
    return(ground_dem_local_proj)
# .............................................


# .............................................
def fn_get_ground_dem_rtree(shp_mjr_axis_ar_buffer_lambert, b_is_feet, crs_line, str_rtree_path):
    # added MAC - 2023.08.29
    # revised - 2023.09.20 - error trap of 'bad' data

    # load the rtree index
    itree = rtree.index.Index(str_rtree_path)

    list_intersecting_dem_paths = []
    list_source_crs = []
    hits = itree.intersection(shp_mjr_axis_ar_buffer_lambert.bounds, objects='raw')
    for int_count, tup_bounds, filepath, str_crs in hits:
        list_intersecting_dem_paths.append(filepath)
        list_source_crs.append(str_crs)
    itree.close()

    b = shp_mjr_axis_ar_buffer_lambert.bounds
    
    # create rio xarray for each tile of the clipped boundary
    list_xar_dem = []
    if len(list_intersecting_dem_paths) > 0:
        for str_dem_filepath in list_intersecting_dem_paths:
            try:
                xar_input_dem = rio.open_rasterio(
                    filename=str_dem_filepath,
                    chunks='auto',
                    parse_coordinates=True,
                    masked=True)
                
                # TODO - 2023.09.20 - run 78 reads a xar with no crs?
                # and therefore can't reproject
                xar_input_dem_lambert  = xar_input_dem.rio.reproject("EPSG:3857")
    
                # read the DEM as a "Rioxarray"
                ground_dem = xar_input_dem_lambert.squeeze()
                list_xar_dem.append(ground_dem)
            except RasterioIOError:
                # added 2023.09.20 - Some img can't be read by rasterio (corrupt files)
                continue # continue to next file if error occurs

        if len(list_xar_dem) > 0:
            # merge the list of rioxarrays
            ground_dem_lambert = merge_arrays(list_xar_dem)
            
            # reproject rioxarray from lambert to input projection
            ground_dem_local_proj = ground_dem_lambert.rio.reproject(crs_line)
            
            if b_is_feet:
                # scale the raster from meters to feet
                ground_dem_local_proj = ground_dem_local_proj * 3.28084
        else:
            ground_dem_local_proj = None
    else:
        ground_dem_local_proj = None
            
    return(ground_dem_local_proj)
# .............................................


# --------------------------------------------
def fn_fix_ground_spikes(gdf_input):
    
    # with asitance from Dr. Joe Rungee (joe.rungee@stantec.com) - 2023.08.31
    list_selected_columns = ['h_distance', 'elev_grnd', 'elev_deck']
    df_fix = gdf_input[list_selected_columns].copy()
    df2_fix = df_fix[df_fix['elev_grnd'] != df_fix['elev_deck']]
    list_ind_vals =  df2_fix.index.to_list()
    
    assess = sorted(set(range(list_ind_vals[0],list_ind_vals[-1])) - set(list_ind_vals))
    
    for ind in assess:
        if any(x not in assess for x in [ind-1, ind+1]):
            # note the hard coded value of 0.5 units in the following code
            if max(abs(df_fix['elev_deck'][ind]-df_fix['elev_deck'][ind-1]), abs(df_fix['elev_deck'][ind]-df_fix['elev_deck'][ind+1])) < 0.5:
                continue
            else:
                df_fix.loc[ind, 'elev_deck'] = np.nan
        elif math.isnan(df_fix.loc[ind-1, 'elev_deck']):
            df_fix.loc[ind, 'elev_deck'] = np.nan
        
    df_fixed = df_fix.interpolate()
        
    # replace the coloumn from df_fixed['elev_deck'] to gdf_input
    gdf_input['elev_deck'] = df_fixed['elev_deck']
    
    return(gdf_input)
# --------------------------------------------


# *********************************************
def fn_get_profile_gdf_on_major_axis_from_dems(shp_mjr_axis_ln,str_mjr_axis_ln_crs,ground_dem_local_proj,deck_dem_local_proj):
    
    # option to turn off the SettingWithCopyWarning
    pd.set_option('mode.chained_assignment', None)
    
    flt_xs_sample_interval = 1 # interval to sample points along a line for cross section - crs units
    total_length = 0

    for i in range(len(shp_mjr_axis_ln.coords)-1):
        start_coords = list(shp_mjr_axis_ln.coords)[i]
        end_coords = list(shp_mjr_axis_ln.coords)[i+1]

        #Get the length of the current road edge
        len_current_edge = fn_distance(start_coords[0],end_coords[0],start_coords[1],end_coords[1])

        x_ls = [start_coords[0]]
        y_ls = [start_coords[1]]

        # create a point at a requested interval - sort of
        n_points = int(len_current_edge // flt_xs_sample_interval)

        if n_points > 0:
            for j in np.arange(1, n_points):
                x_dist = end_coords[0] - start_coords[0]
                y_dist = end_coords[1] - start_coords[1]
                point = [(start_coords[0] + (x_dist/(n_points))*j), (start_coords[1] + (y_dist/(n_points))*j)]
                x_ls.append(point[0])
                y_ls.append(point[1])

        if total_length == 0: # on the first road edge
            # Getting the station - horizontal distance
            df = pd.DataFrame({'x': x_ls,'y': y_ls})
            gdf = gpd.GeoDataFrame(df, geometry = gpd.points_from_xy(df.x, df.y))

            gdf.crs = str_mjr_axis_ln_crs

            gdf['h_distance'] = 0 # Intialize the variable in dataframe

            for index2, row2 in gdf.iterrows():
                gdf['h_distance'].loc[index2] = gdf.geometry[0].distance(gdf.geometry[index2])

            gdf['elev_grnd'] = np.nan # Intialize the variable in dataframe
            gdf['elev_deck'] = np.nan

            for index3, row3 in gdf.iterrows():
                # get the value at nearest point on the rioxarray
                arr_np_raster_val = ground_dem_local_proj.sel(x = row3['x'], y = row3['y'], method="nearest").values
                if arr_np_raster_val.size == 1:
                    gdf['elev_grnd'].loc[index3] = arr_np_raster_val

                arr_np_deck_val = deck_dem_local_proj.sel(x = row3['x'], y = row3['y'], method="nearest").values
                if arr_np_deck_val.size == 1:
                    gdf['elev_deck'].loc[index3] = arr_np_deck_val
            del df

        else: #Any edge of road other than the first edge
            if i == (len(shp_mjr_axis_ln.coords)-2):
                #This is the last edge on the road- add the last point
                x_ls.append(end_coords[0])
                y_ls.append(end_coords[1])

            df_newEdge = pd.DataFrame({'x': x_ls,'y': y_ls})
            gdf_newEdge = gpd.GeoDataFrame(df_newEdge, geometry = gpd.points_from_xy(df_newEdge.x, df_newEdge.y))
            gdf_newEdge.crs = str_mjr_axis_ln_crs

            gdf_newEdge['h_distance'] = 0 #Intialize the variable in dataframe

            for index2, row2 in gdf_newEdge.iterrows():
                gdf_newEdge['h_distance'].loc[index2] = gdf_newEdge.geometry[0].distance(gdf_newEdge.geometry[index2]) + total_length

            gdf_newEdge['elev_grnd'] = np.nan
            gdf_newEdge['elev_deck'] = np.nan

            for index3, row3 in gdf_newEdge.iterrows():
                # get the value at nearest point on the rioxarray
                arr_np_raster_val = ground_dem_local_proj.sel(x = row3['x'], y = row3['y'], method="nearest").values
                if arr_np_raster_val.size == 1:
                    gdf_newEdge['elev_grnd'].loc[index3] = arr_np_raster_val

                arr_np_deck_val = deck_dem_local_proj.sel(x = row3['x'], y = row3['y'], method="nearest").values
                if arr_np_deck_val.size == 1:
                    gdf_newEdge['elev_deck'].loc[index3] = arr_np_deck_val

            gdf = pd.concat([gdf,gdf_newEdge], ignore_index=False)

            del df_newEdge
            del gdf_newEdge

        total_length += len_current_edge

    # reset the index of the gdf
    gdf = gdf.reset_index(drop=True)
    
    # fix the ground spiking
    gdf_fixed = fn_fix_ground_spikes(gdf)

    return(gdf_fixed)
# *********************************************


# ---------------------------------------------
def fn_center_mjr_axis_on_hull(gdf_singlerow):
    
    # added 2023.09.05 to center the line on the hull polygon
    shp_line = gdf_singlerow.iloc[0]['geometry']
    shp_polygon = wkt.loads(gdf_singlerow.iloc[0]['hull_wkt'])
    
    flt_avg_width = float(gdf_singlerow.iloc[0]['avg_width'])
    
    flt_best_difference = float('inf')
    shp_best_split = None
    shp_best_line = None
    
    initial_value = 0.0  # Starting value
    final_value = flt_avg_width  # Ending value
    num_iterations = 100  # Number of iterations
    
    flt_best_offset_left = 0
    
    # ---------- find best left offset value
    # Calculate the step size
    step = (final_value - initial_value) / num_iterations
    current_value = initial_value

    for _ in range(num_iterations):
        shp_offset_line = shp_line.parallel_offset(current_value, 'left')

        # Try to split the polygon with the offset line
        try:
            split_polygons = split(shp_polygon, shp_offset_line)
        except Exception as e:
            pass

        if len(split_polygons.geoms) == 2:
            # Calculate the area difference between the split polygons
            flt_area1 = split_polygons.geoms[0].area
            flt_area2 = split_polygons.geoms[1].area

            flt_difference = abs(flt_area1 - flt_area2)

            if flt_difference < flt_best_difference:
                flt_best_difference = flt_difference
                flt_best_offset_left = current_value
        current_value += step
        
    # ---------- find best right offset value
    # Calculate the step size
    step = (final_value - initial_value) / num_iterations
    current_value = initial_value

    flt_best_offset_right = 0

    for _ in range(num_iterations):
        shp_offset_line = shp_line.parallel_offset(current_value, 'right')

        # Try to split the polygon with the offset line
        try:
            split_polygons = split(shp_polygon, shp_offset_line)
        except Exception as e:
            pass

        if len(split_polygons.geoms) == 2:
            # Calculate the area difference between the split polygons
            flt_area1 = split_polygons.geoms[0].area
            flt_area2 = split_polygons.geoms[1].area

            flt_difference = abs(flt_area1 - flt_area2)

            if flt_difference < flt_best_difference:
                flt_best_difference = flt_difference
                flt_best_offset_right = current_value
        current_value += step
    
    # --- create the 'best' offset and assign to gdf of single row
    if flt_best_offset_right >= flt_best_offset_left:
        str_side = 'right'
        flt_max_value = flt_best_offset_right
    else:
        str_side = 'left'
        flt_max_value = flt_best_offset_left
        
    shp_line_offset = shp_line.parallel_offset(flt_max_value, str_side)
    
    #gdf_singlerow.iloc[0,'geometry'] = shp_line_offset
    gdf_singlerow.iloc[0]['geometry'] = shp_line_offset
    # TODO - there is an error here - copys df but does not change the source df - 2023.09.05
    
    return(gdf_singlerow)
# ---------------------------------------------
 

# ...........................................
def fn_populate_sta_ground_deck_elev(gdf_singlerow):
    
    # center the major axis on the hull polygon
    gdf_singlerow_centered = fn_center_mjr_axis_on_hull(gdf_singlerow)
    
    # given a geodataframe containing a single major axis row
    # get the station, ground elevation and deck elevation
    
    gdf_singlerow = gdf_singlerow_centered
    
    lambert = "epsg:3857"
    
    # get crs of the input geodataframe
    str_mjr_axis_ln_crs = str(gdf_singlerow.crs)
    
    # get variables from the provided row
    flt_mjr_axis = gdf_singlerow.iloc[0]['flt_mjr_axis']
    b_is_feet = gdf_singlerow.iloc[0]['is_feet']
    str_input_cog_ground_dem = gdf_singlerow.iloc[0]['cog_ground_dem']
    int_row_index = gdf_singlerow.index[0]
    shp_mjr_axis_ln = gdf_singlerow.geometry.loc[int_row_index]

    
    int_index_hull = gdf_singlerow.iloc[0]['hull_idx']
    str_major_axis_filepath = gdf_singlerow.iloc[0]['file_path']
 
    # reproject to lambert
    gdf_singlerow_lambert = gdf_singlerow.to_crs(lambert)
    
    path_aoi_folder = os.path.dirname(os.path.dirname(str_major_axis_filepath))
    path_deck_dem = os.path.join(path_aoi_folder, '05_bridge_deck_dems')

    if b_is_feet:
        str_deck_dem_filename = str(int_index_hull) + '_bridge_deck_dem_vert_ft.tif'
    else:
        str_deck_dem_filename = str(int_index_hull) + '_bridge_deck_dem_vert_m.tif'

    path_deck_dem_filepath = os.path.join(path_deck_dem, str_deck_dem_filename)
    
    if os.path.exists(path_deck_dem_filepath):

        # read the bridge deck DEM as a "Rioxarray"
        deck_dem = rio.open_rasterio(path_deck_dem_filepath)

        # reproject the raster to the same projection as the major axis ln
        deck_dem_local_proj = deck_dem.rio.reproject(gdf_singlerow.crs, nodata = np.nan)

        # get geometry of major axis line in lambert
        shp_mjr_axis_ln_lambert = gdf_singlerow_lambert.geometry.loc[int_row_index]

        # buffer the major axis line to an area
        shp_mjr_axis_ar_buffer_lambert = shp_mjr_axis_ln_lambert.buffer(flt_mjr_axis)

        if str_input_cog_ground_dem == 'None':
            # get the ground dem from usgs web service
            ground_dem_local_proj = fn_get_ground_dem_from_usgs_service(shp_mjr_axis_ar_buffer_lambert,
                                                                        b_is_feet,
                                                                        gdf_singlerow.crs)
        else:
            if str_input_cog_ground_dem[:-3] == "tif":
                # 2023.07.24 ... or ... get ground from provided cloud optimized geotiff
                ground_dem_local_proj = fn_get_ground_dem_from_provided_cog(shp_mjr_axis_ar_buffer_lambert,
                                                                            b_is_feet,
                                                                            gdf_singlerow.crs,
                                                                            str_input_cog_ground_dem)
            else:
                # use the rtree (in EPSG:3857) to get the ground dem
                ground_dem_local_proj = fn_get_ground_dem_rtree(shp_mjr_axis_ar_buffer_lambert,
                                                                b_is_feet,
                                                                gdf_singlerow.crs, 
                                                                str_input_cog_ground_dem)
        
        # added 2023.09.20 - error trapping bad dem inputs
        if ground_dem_local_proj is not None:
            # get a pandas dataframe of the ground and deck geometry profile
            gdf = fn_get_profile_gdf_on_major_axis_from_dems(shp_mjr_axis_ln,
                                                             str_mjr_axis_ln_crs,
                                                             ground_dem_local_proj,
                                                             deck_dem_local_proj)
            
            # get a pandas dataframe of the smoothed cross section
            df_smooth_ground_and_deck = fn_get_smooth_deck_and_ground_profile(gdf)
    
            # round all values to two decimal places
            df_smooth_ground_and_deck = df_smooth_ground_and_deck.round(2)
    
            # add the lists as strings to the current row
            str_list_station = str(df_smooth_ground_and_deck['sta'].tolist())
            str_list_ground_elev = str(df_smooth_ground_and_deck['ground_elev'].tolist())
            str_list_max_elev_road_deck = str(df_smooth_ground_and_deck['max_elev_road_deck'].tolist())
    
            # append the values to the dataframe
            gdf_singlerow.at[int_row_index, 'sta'] = str_list_station
            gdf_singlerow.at[int_row_index, 'ground_elv'] = str_list_ground_elev
            gdf_singlerow.at[int_row_index, 'deck_elev'] = str_list_max_elev_road_deck
            
            list_str_columns_to_drop = ['flt_mjr_axis','is_feet','cog_ground_dem']
            gdf_singlerow = gdf_singlerow.drop(columns=list_str_columns_to_drop, axis=1)
        
            return(gdf_singlerow)
        else:
            return(None)
# ...........................................


# --------------------------------------------------------
def fn_attribute_mjr_axis(str_input_dir,int_class,str_input_cog_ground_dem,
                          dict_global_config_data, str_input_json):
    
    """
    Cut a profile of both the ground and the bridge deck dem along each major
    axis line within the area of interest

    Args:
        str_input_dir: path that contains the processed input data 
        folders such as ... 00_input_shapefile ... to ... 07_major_axis_names

    Returns:
        geopackage of the attributed major axis lines
    """
    
    print(" ")
    print("+=================================================================+")
    print("|                  ATTRIBUTE MAJOR AXIS LINES                     |")
    print("|                Created by Andy Carter, PE of                    |")
    print("|             Center for Water and the Environment                |")
    print("|                 University of Texas at Austin                   |")
    print("+-----------------------------------------------------------------+")

    print("  ---(i) PATH TO INPUT FOLDERS: " + str_input_dir)
    print("  ---[c]   Optional: CLASSIFICATION: " + str(int_class) )
    print("  ---[d]   Optional: USER PROVIDED DEM: " + str_input_cog_ground_dem )
    # There is also a global configutation data dictionary
    # There is also a local json configuration
    print("===================================================================")
    
    
    # determine if there is a 'flip_mjr_axis_w_name_ln.shp' file in 07_major_axis_names
    
    wgs = "epsg:4326"

    # from global ini
    flt_mjr_axis = float(dict_global_config_data['08_attribute_mjr_axis']['flt_mjr_axis'])
    
    
    # parse the run's configuration file
    with open(str_input_json) as f:
        json_run_data = json.load(f)
    
    b_is_feet = json_run_data["b_is_feet"]
  
    str_path_to_mjr_axis_shp = os.path.join(str_input_dir, '07_major_axis_names', 'flip_mjr_axis_w_name_ln.shp')
    str_path_to_aoi_folder = os.path.join(str_input_dir, '00_input_shapefile')
    str_aoi_shapefile_path = ''
    
    # find a shapefile in the str_path_to_aoi_folder and get list
    list_shapefiles = fn_filelist(str_path_to_aoi_folder, ('.SHP', '.shp'))
    if len(list_shapefiles) > 0:
        str_aoi_shapefile_path = list_shapefiles[0]
    
    if os.path.exists(str_path_to_mjr_axis_shp):
        if str_aoi_shapefile_path != '' and os.path.exists(str_aoi_shapefile_path):
            # load the shapefile of the major axis lines
            gdf_merge = gpd.read_file(str_path_to_mjr_axis_shp)
            gdf_merge['file_path'] = str_path_to_mjr_axis_shp
            
            # copy geometry to another coloumn
            gdf_merge['str_geom_mjr_ln'] = gdf_merge.geometry.apply(lambda x: wkt.dumps(x))
            
            # load the area of interst polygon
            gdf_area_of_interest = gpd.read_file(str_aoi_shapefile_path)
            
            # TODO - Assumed that the aoi and 'major axis lines' are in same projection - 2022.11.08

            # midpoint of the line
            flt_perct_on_line = 0.5 # midpoint on the line
            gdf_mjr_axis_mid_pt = fn_gdf_point_on_line(flt_perct_on_line, gdf_merge.copy())
            
            # copy and gdf and delete all rows for appending rows later
            gdf_appended_pts = gdf_mjr_axis_mid_pt.copy()
            gdf_appended_pts = gdf_appended_pts[0:0]
            
            # if point in center of linestring is not inside the aoi polygon, drop the row from the merged list
            # pick the first polygon
            shp_aoi_poly = gdf_area_of_interest.iloc[0]['geometry']
            
            for index, row in gdf_mjr_axis_mid_pt.iterrows():
                shp_point = row.geometry
                
                # if point is in polygon (shapely)
                if shp_aoi_poly.contains(shp_point):
                    # append this row to a new geodataframe
                    gdf_new = gpd.GeoDataFrame(pd.DataFrame(row.to_dict(), index=[index]),
                                               geometry="geometry",
                                               crs = gdf_appended_pts.crs)
                
                    gdf_appended_pts = pd.concat([gdf_appended_pts, gdf_new])
                
            gdf_appended_pts.crs = gdf_merge.crs
            
            gdf_appended_ln = gdf_appended_pts.copy()
            
            gdf_appended_ln.geometry = gdf_appended_ln['str_geom_mjr_ln'].apply(loads)
            gdf_appended_ln.drop('str_geom_mjr_ln', axis=1, inplace=True) #Drop WKT column
            
            gdf_appended_ln['mjr_ax_idx'] = gdf_appended_ln.index
            
            # ~~~~~~~~~~~~~~~~~~~~~~~~~~~
            # determine the hull index for each major axis line

            # copy and gdf and delete all rows for appending rows later
            gdf_appended_ln_w_hull_id = gdf_appended_ln.copy()
            gdf_appended_ln_w_hull_id = gdf_appended_ln_w_hull_id[0:0] # a blank geodataframe with fields
            gdf_appended_ln_w_hull_id.crs = gdf_appended_ln.crs
            
            # add the hull_idx field
            gdf_appended_ln_w_hull_id['hull_idx'] = None
            
            # determine the bridge hull ID for each bridge
            arr_unique_files = gdf_appended_ln.file_path.unique()
            
            for item in arr_unique_files:
                
                # select a dataframe of just the rows that have matching filename
                gdf_mjr_axis_per_file = gdf_appended_ln.query("file_path==@item")
                
                # determine if the hull polygon exists
                path_aoi_folder = os.path.dirname(os.path.dirname(item))
                
                str_hull_file_name = 'class_' + str(int_class) + '_ar_3857.shp'
                path_hull_shp_file = os.path.join(path_aoi_folder, '02_shapefile_of_hulls', str_hull_file_name)
                
                if os.path.exists(path_hull_shp_file):
                    
                    gdf_bridge_hull = gpd.read_file(path_hull_shp_file)
                    # convert the shapefile of the hull to the crs of the gdf_mjr_axis_per_file
                    gdf_bridge_hull_reproject = gdf_bridge_hull.to_crs(gdf_mjr_axis_per_file.crs)
                
                    for index, row in gdf_mjr_axis_per_file.iterrows():
                        # get the geometry of the current major axis
                        shp_current_mjr_axis = row['geometry']
                        int_found_hull_index = -99
                        
                        for index2, row2 in gdf_bridge_hull_reproject.iterrows():
                            # get geometry of current hull
                            shp_current_hull = row2['geometry']
                            
                            shp_intersection = shp_current_hull.intersection(shp_current_mjr_axis)
                            if shp_intersection.wkt != "LINESTRING EMPTY":
                            #if shp_intersection.wkt != "LINESTRING Z EMPTY":
                                # the bridge hull intersects the major axis
                                int_found_hull_index = index2
                                
                        # append the 'hull_idx' to the 'row_out' pandas series
                        s_hull_idx = pd.Series([int_found_hull_index], index=['hull_idx'])
                        
                        # combine row and s_hull_idx dataseries (converted to dict)
                        dict_row_out = row.to_dict() | s_hull_idx.to_dict()

                        # append this row to a new geodataframe
                        gdf_row_out = gpd.GeoDataFrame(pd.DataFrame(dict_row_out, index=[0]),
                                                       geometry="geometry",
                                                       crs = gdf_appended_ln_w_hull_id.crs)

                        # append the row_out to the gdf_appended_ln_w_hull_id
                        gdf_appended_ln_w_hull_id = pd.concat([gdf_appended_ln_w_hull_id, gdf_row_out])
                        
                    #print(gdf_appended_ln_w_hull_id)
            gdf_appended_ln_w_hull_id = gdf_appended_ln_w_hull_id.reset_index(drop=True)
            

            # ~~~~~~~~~~~~~~~~~~~~~~~~~~

            # ----------------------

            gdf_appended_ln_w_hull_id.crs = gdf_appended_ln.crs
            
            
            # create empty coloumns
            gdf_appended_ln_w_hull_id['sta'] = ''
            gdf_appended_ln_w_hull_id['ground_elv'] = ''
            gdf_appended_ln_w_hull_id['deck_elev'] = ''
            
            # adding values for multi-processing
            gdf_appended_ln_w_hull_id['is_feet'] = b_is_feet
            gdf_appended_ln_w_hull_id['flt_mjr_axis'] = flt_mjr_axis
            gdf_appended_ln_w_hull_id['cog_ground_dem'] = str_input_cog_ground_dem  
            
            l = len(gdf_appended_ln_w_hull_id)
            
            # TODO: add the hull geometry here - 2023.09.05
            # add the hull geometry to the record
            gdf_appended_ln_w_hull_id = fn_add_hull_geometry_early(gdf_appended_ln_w_hull_id, int_class)
            
            # --------------
            num_processors = (mp.cpu_count() - 1)
            pool = Pool(processes = num_processors)
            
            # create a list of geodataframes where each item contains one row
            list_of_single_row_gdfs = [gpd.GeoDataFrame([row], crs=gdf_appended_ln_w_hull_id.crs) for _, row in gdf_appended_ln_w_hull_id.iterrows()]
    
            list_gdfs = list(tqdm.tqdm(pool.imap(fn_populate_sta_ground_deck_elev, list_of_single_row_gdfs),
                                       total = l,
                                       desc='Profile',
                                       bar_format = "{desc}:({n_fmt}/{total_fmt})|{bar}| {percentage:.1f}%",
                                       ncols=67 ))

            pool.close()
            pool.join()
            
            # added 2023.09.20 - Drop the 'None' values
            list_gdfs = [item for item in list_gdfs if item is not None]
            
            if len(list_gdfs) > 0:
                gdf_appended_ln_w_hull_id = gpd.GeoDataFrame(pd.concat(list_gdfs), crs=list_gdfs[0].crs)
                
                # --------------
    
                # ---------------------------
                # add the lat/long of the centerpoint of the major axis line
                gdf_mjr_axis_ln_wgs = gdf_appended_ln_w_hull_id.to_crs(wgs)
                
                # add the lat/long coloumns
                gdf_appended_ln_w_hull_id['latitude'] = ''
                gdf_appended_ln_w_hull_id['longitude'] = ''
                
                for index, row in gdf_mjr_axis_ln_wgs.iterrows():
                    # -------lat / Long Coordinates of Major Axis Centeroid -----
                    geom_wkt = row['geometry']
                    str_lon = str(round(geom_wkt.centroid.coords[0][0], 4))
                    str_lat = str(round(geom_wkt.centroid.coords[0][1], 4))
                    
                    # append the lat and long
                    gdf_appended_ln_w_hull_id.at[index, 'latitude'] = str_lat
                    gdf_appended_ln_w_hull_id.at[index, 'longitude'] = str_lon
                # ---------------------------
                
                # add the 'run' attributes to the geodataframe
                str_aoi_name = json_run_data["str_aoi_name"]
                str_copc_name = json_run_data["copc_point_cloud"]["copc_name"]
                int_copc_date = json_run_data["copc_point_cloud"]["copc_date"]
                str_copc_shortname = json_run_data["copc_point_cloud"]["copc_short_name"]
                
                gdf_appended_ln_w_hull_id['aoi_name'] = str_aoi_name 
                gdf_appended_ln_w_hull_id['copc_name'] = str_copc_name 
                gdf_appended_ln_w_hull_id['copc_date'] = int_copc_date
                gdf_appended_ln_w_hull_id['copc_name_s'] = str_copc_shortname
    
                str_path_xs_folder = os.path.join(str_input_dir, '08_cross_sections')
                
                # create the output directory if it does not exist
                os.makedirs(str_path_xs_folder, exist_ok=True)
                
                str_major_axis_xs_file = os.path.join(str_path_xs_folder, '08_01_mjr_axis_xs.gpkg')
                
                # export the geopackage
                gdf_appended_ln_w_hull_id.to_file(str_major_axis_xs_file, driver='GPKG')
                
                # --- running the additional sub-processes for additional attribution
                # Assign the feature line id
                fn_assign_feature_id_to_mjr_axis(str_input_dir,dict_global_config_data)
                
                # assign the National Bridge Inventory here
                str_nbi_point_filepath = dict_global_config_data['global_input_files']['str_texas_nbi_filepath']
                fn_conflate_nbi(str_input_dir, str_nbi_point_filepath)
                
                # compute the low chord attributes
                fn_compute_low_chord_attributes(str_input_dir)
                
                # add the hull geometry to the record
                fn_add_hull_geometry(str_input_dir, int_class)
                
                # add the HAND rating curves to each bridge
                str_hand_stream_ln_gpkg = dict_global_config_data['global_input_files']['str_hand_stream_ln_gpkg']
                str_hydro_table_parquet = dict_global_config_data['global_input_files']['str_hydro_table_parquet']
                str_segment_field_name = dict_global_config_data['global_input_files']['str_segment_field_name']
                
                fn_fetch_hand_rating_curves(str_input_dir,str_hand_stream_ln_gpkg,str_hydro_table_parquet,str_segment_field_name)
                
                # plot the cross sections
                str_flow_csv_filename = 'NONE'
                str_majr_axis_filename = '08_08_mjr_axis_xs_w_feature_id_nbi_low_hull_rating.gpkg' # created from fn_add_hull_geometry
                
                fn_plot_cross_sections(str_input_dir,str_majr_axis_filename,str_flow_csv_filename)
                
                # generate the kml
                fn_generate_kml(str_input_dir,str_input_json,dict_global_config_data)
            else:
               print("  COMPLETE: No major axis lines found within area of interest ") 
    
        else:
            print("  ERROR: Area of Interest not found in: " + str_path_to_aoi_folder)
    else:
        print("  ERROR: Required Files Not Found: " + str_path_to_mjr_axis_shp)
    
# --------------------------------------------------------


#~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
if __name__ == '__main__':

    flt_start_run = time.time()
    
    parser = argparse.ArgumentParser(description='==================== CUT MAJOR-AXIS PROFILES ======================')
    
    parser.add_argument('-i',
                        dest = "str_input_dir",
                        help=r'REQUIRED: input directory of processed data for area of interest [contains 00 to 07 folders]: Example: C:\bridge_data\folder_location',
                        required=True,
                        metavar='DIR',
                        type=str)
    
    parser.add_argument('-c',
                        dest = "int_class",
                        help='OPTIONAL: desired point cloud classification: Default=17 (bridge)',
                        required=False,
                        default=17,
                        metavar='INTEGER',
                        type=int)
    
    parser.add_argument('-d',
                        dest = "str_input_cog_ground_dem",
                        help=r'OPTIONAL: path to bare earth ground COG geotiff  Example: D:\037_georgetown\huc_12070205_bridge\dem_merge\dem_merge.tif',
                        required=False,
                        defalut='None',
                        metavar='FILE',
                        type=str)
    
    parser.add_argument('-g',
                        dest = "str_global_config_ini_path",
                        help=r'OPTIONAL: directory to national water model input flowlines: Example: G:\X-NWS\X-National_Datasets\nwm_flows.gpkg',
                        required=False,
                        default=r'C:\Users\civil\dev\tx-bridge\src\config_global.ini',
                        metavar='FILE',
                        type=str)
    
    parser.add_argument('-i',
                        dest = "str_input_json",
                        help=r'REQUIRED: path to the input coniguration json Example: D:\bridge_local_test\config.json',
                        required=True,
                        metavar='FILE',
                        type=str)
    
    args = vars(parser.parse_args())
    
    str_input_dir = args['str_input_dir']
    int_class = args['int_class']
    str_input_cog_ground_dem = args['str_input_cog_ground_dem']
    str_global_config_ini_path = args['str_global_config_ini_path']
    str_input_json = args['str_input_json']
    
    # convert the INI to a dictionary
    dict_global_config_data = json.loads(fn_json_from_ini(str_global_config_ini_path))

    fn_attribute_mjr_axis(str_input_dir, int_class,str_input_cog_ground_dem,dict_global_config_data,str_input_json)
    
    flt_end_run = time.time()
    flt_time_pass = (flt_end_run - flt_start_run) // 1
    time_pass = datetime.timedelta(seconds=flt_time_pass)
    
    print('Compute Time: ' + str(time_pass))
    #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
