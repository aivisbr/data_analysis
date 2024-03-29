# Script generated by ChatGPT-4 + added some additional changes

import pandas as pd 
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
import requests # to get GeoJSON data from data.gov.lv
import geopandas as gpd

# PROMPT #1
"""Perform an exploratory data analysis about these data."""

# Load and preprocess the dataset
filepath = '/vps_ce_kopv_limeni_vsk_2022_2023.csv' # change filepath where csv file is located

data = pd.read_csv(filepath, delimiter=';')
data['Vidējais, %'] = pd.to_numeric(data['Vidējais, %'].str.replace(',', '.').replace('#DIV/0!', np.nan), errors='coerce')

# Display the first few rows of the dataset and the column names to understand its structure
data_head = data.head()
data_columns = data.columns.tolist()

data_head, data_columns

# General statistics
general_stats = data.describe()

# Missing values
missing_values = data.isnull().sum()

general_stats, missing_values

# Set the aesthetic style of the plots
sns.set_style("whitegrid")

# EDA: Relationship between average scores and number of exam takers
plt.figure(figsize=(10, 6))
sns.scatterplot(data=data, x='Vidējais, %', y='Kārtotāju skaits')
plt.title('Relationship between Average Scores and Number of Exam Takers')
plt.xlabel('Average Score (%)')
plt.ylabel('Number of Exam Takers')
plt.show()

# Distribution of average scores
plt.figure(figsize=(10, 6))
sns.histplot(data=data, x='Vidējais, %', bins=30, kde=True)
plt.title('Distribution of Average Scores')
plt.xlabel('Average Score (%)')
plt.ylabel('Frequency')
plt.show()

# PROMPT #2
"""
Prepare list of TOP 5 `Izsglītības iestādes nosaukums` which has a largest average `Vidējais, %`
where `Pārbaudījums` LIKE `Eksāmens matemātikā%`. You need to weight average with `Kārtotāju skaits`
"""

# Filter data for 'Eksāmens matemātikā%' and then calculate the weighted average of 'Vidējais, %'
math_exams_data = data[data['Pārbaudījums'].str.startswith('Eksāmens matemātikā')]

# Compute the weighted average
math_exams_data['Weighted Average'] = math_exams_data['Vidējais, %'] * math_exams_data['Kārtotāju skaits']
weighted_avg = math_exams_data.groupby('Izsglītības iestādes nosaukums').sum()
weighted_avg['Weighted Average'] = weighted_avg['Weighted Average'] / weighted_avg['Kārtotāju skaits']

# Get the top 5 schools with the highest weighted average
top_5_schools = weighted_avg['Weighted Average'].sort_values(ascending=False).head(5)
top_5_schools


# PROMPT #3
"""What about TOP 5 lowest scores?"""

# Get the bottom 5 schools with the lowest weighted average
bottom_5_schools = weighted_avg['Weighted Average'].sort_values(ascending=True).head(5)
bottom_5_schools

# PROMPT #5
"""
How strong is a correlation between maths scores and physics  (`Pārbaudījums` like `Eksāmens fizikā%`) ?
"""

# Filter data for 'Eksāmens fizikā%' and compute the weighted average
physics_exams_data = data[data['Pārbaudījums'].str.startswith('Eksāmens fizikā')]
physics_exams_data['Weighted Average'] = physics_exams_data['Vidējais, %'] * physics_exams_data['Kārtotāju skaits']
weighted_avg_physics = physics_exams_data.groupby('Izsglītības iestādes nosaukums').sum()
weighted_avg_physics['Weighted Average'] = weighted_avg_physics['Weighted Average'] / weighted_avg_physics['Kārtotāju skaits']

# Merge the math and physics datasets on the educational institution name
merged_data = pd.merge(
    weighted_avg[['Weighted Average']],
    weighted_avg_physics[['Weighted Average']],
    on='Izsglītības iestādes nosaukums',
    suffixes=('_math', '_physics')
)

# Calculate the Pearson correlation coefficient
correlation = merged_data.corr().iloc[0, 1]
correlation

# PROMPT #6
"""
Create a scatter plot with TOP 100 schools with highest scores in maths on x-axis and this schools' scores in physics on y-axis.
If school doesn't have a score in physics then replace NaN with 0.
"""

# Get the top 100 schools based on weighted average scores in mathematics
top_100_math_schools = weighted_avg['Weighted Average'].sort_values(ascending=False).head(100)

# Merge with the physics scores
top_100_merged_data = pd.merge(
    top_100_math_schools,
    weighted_avg_physics[['Weighted Average']],
    on='Izsglītības iestādes nosaukums',
    suffixes=('_math', '_physics'),
    how='left'
)

# Replace NaN physics scores with 0
top_100_merged_data['Weighted Average_physics'].fillna(0, inplace=True)

# Generate the scatter plot
plt.figure(figsize=(12, 8))
sns.scatterplot(
    data=top_100_merged_data,
    x='Weighted Average_math',
    y='Weighted Average_physics'
)
plt.title('Math vs Physics Scores for Top 100 Schools in Math')
plt.xlabel('Math Scores')
plt.ylabel('Physics Scores')
plt.grid(True)
plt.show()

# PROMPT #7
"""
Give me a list of schools which has an average maths score greater than 60, but doesn't have score in physics.
Add sum of `Kārtotāju skaits` in maths for each school.
"""

# Filter schools with an average math score greater than 60 and no physics score
schools_high_math_no_physics = top_100_merged_data[
    (top_100_merged_data['Weighted Average_math'] > 60) & 
    (top_100_merged_data['Weighted Average_physics'] == 0)
]

# Merge back with math exams data to get the sum of 'Kārtotāju skaits' for each school
schools_high_math_no_physics = pd.merge(
    schools_high_math_no_physics,
    math_exams_data.groupby('Izsglītības iestādes nosaukums')['Kārtotāju skaits'].sum(),
    on='Izsglītības iestādes nosaukums'
)

# Rename columns for clarity and display the result
schools_high_math_no_physics.rename(
    columns={
        'Weighted Average_math': 'Average Math Score',
        'Kārtotāju skaits': 'Total Exam Takers in Math'
    }, 
    inplace=True
)

schools_high_math_no_physics[['Average Math Score', 'Total Exam Takers in Math']]

# PROMPT #8
"""
Create a choropleth map which shows average score in maths in `Pašvaldība` (Latvian cities and municipalities).
"""

url = 'https://data.gov.lv/dati/dataset/7bb04db9-97ce-4a30-b93a-10ba8dafd104/resource/3ded58bd-c0dc-419a-97ff-59ba45a7b1b0/download/administrativas_teritorijas_2021.geojson'
response = requests.get(url)
# Save GeoJSON file on local computer
# with open('novadi.geojson', 'wb') as file:
#     file.write(response.content)
latvia_geo = gpd.GeoDataFrame.from_features(response.json()['features'])

# Load the GeoJSON file containing the geographic boundaries of Latvian municipalities
    # If GeoJSON file is already downloaded
#geojson_path = '/novadi.geojson'
#latvia_geo = gpd.read_file(geojson_path)

# Compute the average math score for each municipality
math_scores_by_municipality = math_exams_data.groupby('Pašvaldība')['Vidējais, %'].mean()

# Merge the GeoJSON data with the average math scores
latvia_geo = latvia_geo.merge(math_scores_by_municipality, left_on='NOSAUKUMS', right_on='Pašvaldība', how='left')

# Check the merged data
latvia_geo.head()

# Plotting the choropleth map
latvia_geo.plot(column='Vidējais, %', legend=True, figsize=(10, 10), cmap='OrRd', missing_kwds={'color': 'lightgrey'})
plt.title('Average Math Scores by Municipality in Latvia')
plt.xlabel('Longitude')
plt.ylabel('Latitude')
plt.show()