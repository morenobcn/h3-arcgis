from itertools import chain
import json
import os
from pathlib import Path

from arcgis.features import GeoAccessor
from arcgis.geometry import Geometry, Polygon
from arcgis.gis import GIS
from copy import deepcopy
from dotenv import load_dotenv, find_dotenv
from h3 import h3
import pandas as pd
import swifter


# what we're going to tell the world about
__all__ = ['get_unique_h3_ids_for_aoi', 'get_esri_geometry_for_h3_id', 'get_esri_geometry_for_h3_id', 
           'get_h3_hex_dataframe_from_h3_id_lst', 'get_h3_hex_for_aoi', 'get_nonoverlapping_h3_hexbins_for_points']


def _preprocess_sdf_for_h3(orig_df:pd.DataFrame)->pd.DataFrame:
    """
    Since H3 cannot handle multipart geometries, break apart any potential multipart geometries into
    discrete rows for subsequent analysis.
    :param orig_df: Spatially enabled dataframe delineating the area of interest.
    :returns: Spatially enabled dataframe of hexagons covering the area of interest.
    """
    # create a new dataframe to hold single part geometric features
    new_df = pd.DataFrame(columns=orig_df.columns)

    # for every row explode out any multipart geometric features since Uber H3 cannot hadle multipart
    for x, row in orig_df.iterrows():
        geom = row['SHAPE']
        for ring in geom.rings:
            new_geom = deepcopy(geom)
            new_geom.rings = [ring]
            new_row = deepcopy(row)
            new_row['SHAPE'] = new_geom
            new_df = new_df.append(new_row)

    # reset the index and get the geometry working
    new_df.reset_index(drop=True, inplace=True)
    new_df.spatial.set_geometry('SHAPE')
    
    return new_df


def get_unique_h3_ids_for_aoi(orig_df:pd.DataFrame, hex_level:int=9)->list:
    """
    Given an area of interest, create Uber H3 hexagons covering the area of interest.
    :param orig_df: Spatially enabled dataframe delineating the area of interest.
    :returns: List of unique Uber H3 indicies whose centroids fall within the area of interest.
    """
    # explode all the multipart geometric features into discrete geometric features
    expl_df = _preprocess_sdf_for_h3(orig_df)
    
    # create a feature set of all the exploded features
    fs_expl = expl_df.spatial.to_featureset()

    # extract the features as geojson from the feature set
    fs_geojson = json.loads(fs_expl.to_geojson)
    geojson_feat_lst = fs_geojson['features']

    def _get_h3_ids(geojson_feat):
        """helper function to get the h3 id's for a single geometry"""

        # pull the geometry out of the geojson feature
        geojson_aoi = geojson_feat['geometry']

        # get a list of all the hex id's within the area of interest
        return h3.polyfill(geojson_aoi, hex_level, geo_json_conformant=True)

    # get a list of lists containing all the h3 id's contained in the area of interest
    h3_ids_lst = [_get_h3_ids(feat) for feat in geojson_feat_lst]

    # combine all the id's and use a set to eliminate any duplicates
    h3_ids = set(chain.from_iterable(h3_ids_lst))
    
    if len(h3_ids) == 0:
        raise Exception(f'The resolution provided, H3 level {hex_level}, is too coarse to return any results.'
                       f' Please select a finer level of detail, say level {hex_level+1}, and see if that works '
                        'any better.')
    
    return list(h3_ids)


def get_esri_geometry_for_h3_id(h3_id:str)->Polygon:
    """
    Convert an Uber H3 id to an ArcGIS Python API Polygon Geometry object.
    :param h3_id:String Uber H3 id.
    :return: ArcGIS Python API Polygon Geometry object
    """
    # get a list of coordinate rings for the hex id's
    coord_lst = [h3.h3_to_geo_boundary(h3_id, geo_json=True)]

    # creat a geometry object using this geometry list
    return Geometry({"type" : "Polygon", "coordinates": coord_lst, 'spatialReference': {'wkid': 4326}})


def get_h3_hex_dataframe_from_h3_id_lst(h3_id_lst:list)->pd.DataFrame:
    """
    From a list of H3 id's, return a spatially enabled dataframe with all the geometries.
    :param h3_id_lst: STring list of H3 identifiers.
    :return: Spatially enabled dataframe of hexagons.
    """
    # create a list of geometries corresponding to the hex id's
    geom_lst = [get_esri_geometry_for_h3_id(hex_id) for hex_id in h3_id_lst]

    # zip together the hex id's and geometries into a dataframe, and spatially enable it
    df = pd.DataFrame(zip(h3_id_lst, geom_lst), columns=['h3_id', 'SHAPE'])
    df.spatial.set_geometry('SHAPE')
    
    return df

    
def get_h3_hex_for_aoi(orig_df:pd.DataFrame, hex_level:int=9)->pd.DataFrame:
    """
    Given an area of interest, create Uber H3 hexagons covering the area of interest.
    :param orig_df: Spatially enabled dataframe delineating the area of interest.
    :return: Spatially enabled dataframe of hexagons covering the area of interest.
    """
    h3_ids = get_unique_h3_ids_for_aoi(orig_df, hex_level)
    df = get_h3_hex_dataframe_from_h3_id_lst(h3_ids)
    return df


# consistent column names - may parameterize later, but this works for now...
h3_id_col = 'h3_id'
h3_lvl_col = 'h3_lvl'
h3_lvl_orig_col = 'h3_orig'


def _h3_col(h3_lvl):
    """Make it easy and reporducable to create a h3 column"""
    return f'h3_{h3_lvl:02d}'


def _get_h3_range_lst(h3_min, h3_max):
    """Helper to get H3 range list."""
    return list(range(h3_min, h3_max + 1))


def _get_h3_range_lst_from_df(df):
    """Helper to get H3 range list from column names."""
    return [int(col[-2:]) for col in df.columns if col.startswith('h3_') and col[-2:].isnumeric()]


def add_h3_ids_to_points(df: pd.DataFrame, h3_max: int, h3_min: int) -> pd.DataFrame:
    """Add Uber H3 ids to the point geometries in a Saptailly Enabled DataFrame.
    :param df: Spatially Enabled DataFrame with point geometries to be aggregated.
    :param h3_max: Integer maximum H3 grid level defining the samllest geographic hex area - must be larger than the minimum.
    :param h3_min: Integer minimum H3 grid level defining the largest geograhpic hex area - must be smaller than the maximum.
    :return: Pandas DataFrame with Uber H3 ids added for all the resolutions betwen teh maximum and minimum.
    """
    assert h3_max > h3_min

    # get a list of zoom levels and ensure the H3 levels are sorted from highest to lowest resolution
    h3_lvl_lst = _get_h3_range_lst(h3_min, h3_max)
    h3_lvl_lst.sort(reverse=True)

    # calculate the highest resolution H3 id for each location
    first_level = h3_lvl_lst[0]
    df[_h3_col(first_level)] = df.SHAPE.swifter.apply(
        lambda geom: h3.geo_to_h3(geom.centroid[1], geom.centroid[0], first_level))

    # use the highest resolution H3 id to get progressivley lower resolution H3 id's
    for h3_lvl in h3_lvl_lst[1:]:
        df[_h3_col(h3_lvl)] = df[_h3_col(first_level)].swifter.apply(
            lambda first_val: h3.h3_to_parent(first_val, h3_lvl))

    return df


def get_h3_ids_by_point_count(df: pd.DataFrame, min_count: int) -> pd.DataFrame:
    """Assign a H3 id to each point based on the H3 count in increasingly resolution H3 hexagons. If not enough present, the points
    for the summary tesselation cell will not be retained.
    :param df: Pandas DataFrame with properly formatted columns delineating the Uber H3 ids at varying resolutions.
    :param min_count: The minimum count of points to consider for retaining the H3 id.
    """
    # get the levels from the column names
    h3_lvl_lst = _get_h3_range_lst_from_df(df)

    # ensure the hex levels are sorted in order (larger to smaller area)
    h3_lvl_lst.sort()

    # iterate the hex levels
    for h3_lvl in h3_lvl_lst:
        # create the hex column name string
        h3_col = _h3_col(h3_lvl)

        # get the count of every hex id at this resolution
        h3_id_cnt = df[h3_col].value_counts()

        # if the count for the hex id is greater than the minimum, assign an id - critical for PII
        h3_id_lst = h3_id_cnt[h3_id_cnt > min_count].index.values

        # create a slice expression for just records matching the saved hex ids
        df_slice = df[h3_col].isin(h3_id_lst)

        # save the hex id's in the final column for the values exceeding the threshold
        df.loc[df_slice, h3_id_col] = df[df_slice][h3_col]
        df.loc[df_slice, h3_lvl_col] = h3_lvl

        # Note the hex level
        df.loc[df_slice, h3_lvl_orig_col] = h3_lvl

    # drop values not meeting the threshold - this is the key step for protecting PII
    df.dropna(subset=[h3_id_col, h3_lvl_col], inplace=True)

    return df


def remove_overlapping_h3_ids(df: pd.DataFrame) -> pd.DataFrame:
    """Remove all overlapping H3 ids. This assigns points to a larger parent H3 region if other points in this
    parent hexagon were not dense enough to populate the other six participating hexbins.
    :param df: Pandas DataFrame with standard schema produced by earlier steps.
    :return: Pandas DataFrame ready to create geometries and counts with.
    """
    # get the levels from the column names
    h3_lvl_lst = _get_h3_range_lst_from_df(df)

    # ensure reverse sorting, so moving from smaller to larger h3 hexbins
    h3_lvl_lst.sort(reverse=True)

    # get a list of all h3 id's for comparison later
    h3_id_lst = df[h3_id_col].unique()

    # reverse the sorting so it goes from smallest area to largest
    for h3_lvl in h3_lvl_lst[:-1]:
        # get the name of the column at the next zoom level
        nxt_h3_lvl = h3_lvl - 1
        nxt_h3_col = _h3_col(nxt_h3_lvl)

        # create a filter to identify only the records where the next zoom level, the containing h3,
        # is also being used
        df_slice = df[nxt_h3_col].isin(h3_id_lst)

        # if the hexbin id is present at a larger extent zoom level, inherit it
        df.loc[df_slice, h3_id_col] = df[nxt_h3_col]
        df.loc[df_slice, h3_lvl_col] = nxt_h3_lvl

    return df


def get_h3_hexbins_with_counts(df: pd.DataFrame, h3_id_col: str = 'h3_id') -> pd.DataFrame:
    """Convert the points with designated Uber H3 ids to hexbins with counts.
    :param df: Pandas DataFrame with H3 ids in a designated column.
    :param h3_id: Column containing the Uber H3 id for each point.
    :return: Pandas Spatially Enabled Dataframe of hexagon polygons and point counts.
    """
    # get the count for each hex id
    h3_id_cnt = df[h3_id_col].value_counts()
    h3_id_cnt.name = 'count'

    # get the geometries for all the count hex ids
    hex_df = get_h3_hex_dataframe_from_h3_id_lst(h3_id_cnt.index)

    # get the hex levels
    level_df = df[[h3_id_col, h3_lvl_col]].drop_duplicates().set_index(h3_id_col)

    # join the counts to the geometry
    out_df = hex_df.join(h3_id_cnt, on=h3_id_col).join(level_df, on=h3_id_col)

    return out_df


def get_nonoverlapping_h3_hexbins_for_points(sdf: pd.DataFrame, h3_min: int = 5, h3_max: int = 9,
                                             min_count: int = 100) -> pd.DataFrame:
    """Get points summarized by non-overlapping Uber H3 hexbins at mulitiple resolution levels while
    ensuring the minimum count of points is retained in each hexbin. If enough points are not members
    of the lowest level of resolution, this function will continue to aggregate up to the next larger
    hexbin resolution until the largest area (smaller number) is reached. Hexbin areas still without
    enough points to meet the minimum threshold will not be represented.
    :param df: Spatially Enabled DataFrame with point geometries to be aggregated.
    :param h3_max: Integer maximum H3 grid level defining the samllest geographic hex area - must be larger than the minimum.
    :param h3_min: Integer minimum H3 grid level defining the largest geograhpic hex area - must be smaller than the maximum.
    :param min_count: Minimum point count for a grid.
    """
    # add h3 ids
    df = add_h3_ids_to_points(sdf, h3_max, h3_min)

    # assign h3 grid based on count, and drop those blow the threshold
    cnt_df = get_h3_ids_by_point_count(df, min_count)

    # roll up smaller grid id assignments to larger parent hexbins
    cln_cnt_df = remove_overlapping_h3_ids(cnt_df)

    # convert to geometry with count
    out_df = get_h3_hexbins_with_counts(cln_cnt_df)

    return out_df