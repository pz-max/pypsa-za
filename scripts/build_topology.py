# coding: utf-8

import networkx as nx
import pandas as pd
import geopandas as gpd
from shapely.geometry import LineString
import numpy as np
import rasterstats
from operator import attrgetter
import pypsa
from vresutils.costdata import annuity
from vresutils.shapes import haversine
import os

def save_to_geojson(df, fn):
    if os.path.exists(fn):
        os.unlink(fn)  # remove file if it exists
    if not isinstance(df, gpd.GeoDataFrame):
        df = gpd.GeoDataFrame(dict(geometry=df))

    # save file if the GeoDataFrame is non-empty
    if df.shape[0] > 0:
        df = df.reset_index()
        schema = {**gpd.io.file.infer_schema(df), "geometry": "Unknown"}
        df.to_file(fn, driver="GeoJSON", schema=schema)
    else:
        # create empty file to avoid issues with snakemake
        with open(fn, "w") as fp:
            pass


def convert_lines_to_gdf(lines,centroids):
    gdf = gpd.GeoDataFrame(lines)
    linestrings = []
    for i, row in lines.iterrows():
        point1 = centroids[row['bus0']]
        point2 = centroids[row['bus1']]
        linestring = LineString([point1, point2])
        linestrings.append(linestring)
    gdf['geometry']=linestrings
    return gdf

def check_centroid_in_region(regions,centroids):
    idx = regions.index[~centroids.intersects(regions['geometry'])]
    buffered_regions = regions.buffer(-0.1)
    boundary = buffered_regions.boundary
    for i in idx:
        # Initialize a variable to store the minimum distance
        min_distance = np.inf

        # Iterate over a range of distances along the boundary
        for d in np.arange(0, boundary[i].length, 0.01):
            # Interpolate a point at the current distance
            p = boundary[i].interpolate(d)
            # Calculate the distance between the centroid and the interpolated point
            distance = centroids[i].distance(p)
            # If the distance is less than the minimum distance, update the minimum distance and the closest point
            if distance < min_distance:
                min_distance = distance
                closest_point = p
        centroids[i] = closest_point
    return centroids


def build_topology():
    # Load supply regions and calculate population per region
    regions = gpd.read_file(snakemake.input.supply_regions).set_index('name')[['geometry']]
    centroids = regions['geometry'].centroid #TODO check against original given warning about CRS
    centroids = check_centroid_in_region(regions,centroids)

    # Find edges between touching regions using spatial join
    lines = gpd.sjoin(regions, regions, op='touches')['index_right']
    lines = lines.reset_index()
    lines.columns = ['bus0', 'bus1']

    # Calculate length of lines
    def haversine_length(row):
        lon1, lat1, lon2, lat2 = map(np.radians, [centroids[row['bus0']].x, centroids[row['bus0']].y, centroids[row['bus1']].x, centroids[row['bus1']].y])
        dlon = lon2 - lon1
        dlat = lat2 - lat1
        a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
        c = 2 * np.arcsin(np.sqrt(a))
        return c * 6371

    # If lines is empty, return empty dataframes
    if lines.empty:
        lines = pd.DataFrame(index=[],columns=['name','bus0','bus1','length','num_parallel'])
    else:
        lines['length'] = lines.apply(haversine_length, axis=1) * snakemake.config['lines']['length_factor']
               
    # Initialize buses dataframe
    line_config = snakemake.config['lines']
    v_nom = line_config['v_nom']
    buses = (regions
             .assign(
                 x=centroids.map(attrgetter('x')),
                 y=centroids.map(attrgetter('y')),
                 v_nom=v_nom)
            )

    # Calculate population in each region
    population = pd.DataFrame(rasterstats.zonal_stats(regions['geometry'], snakemake.input.population, stats='sum'))['sum']
    population.index = regions.index
    buses['population'] = population

    # Load num_parallel data and calculate num_parallel column for lines dataframe
    num_lines = pd.read_excel(snakemake.input.num_lines,
                        sheet_name = snakemake.wildcards.regions, 
                        index_col=0).set_index(['bus0', 'bus1'])
    num_parallel = sum(num_lines['num_parallel_{}'.format(int(v))] * (v/v_nom)**2
                    for v in (275, 400, 765))
    if not lines.empty:
        lines = (lines
                    .join(num_parallel.rename('num_parallel'), on=['bus0', 'bus1'])
                    .join(num_parallel.rename("num_parallel_i"), on=['bus1', 'bus0']))

        lines['num_parallel'] = (line_config['s_nom_factor'] * lines['num_parallel']
                                    .fillna(lines.pop('num_parallel_i')))
        lines.reset_index(drop=True,inplace=True)

        lines = convert_lines_to_gdf(lines,centroids)

    return buses, lines

if __name__ == "__main__":
    if 'snakemake' not in globals():
        from _helpers import mock_snakemake
        snakemake = mock_snakemake('build_topology', 
                            **{'costs':'ambitions',
                            'regions':'27-supply',
                            'resarea':'redz',
                            'll':'copt',
                            'opts':'LC-24H',
                            'attr':'p_nom'})

    buses, lines = build_topology()
    save_to_geojson(buses,snakemake.output.buses)
    
    if not lines.empty:
        save_to_geojson(lines,snakemake.output.lines)
    else:
        save_to_geojson(buses,snakemake.output.lines) # Dummy file will not get used if single node model  
