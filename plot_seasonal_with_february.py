#!/usr/bin/env python3
"""
Create PM2.5 seasonal trend plots including February data
Shows trends by season/year for each station
"""
import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
from pathlib import Path
import seaborn as sns

# Set style
plt.style.use('default')
sns.set_palette("husl")

# Define input directory
input_dir = Path('/mnt/c/Users/Deepak iFOREST/OneDrive - iforest.global/mumbai_CPCB_staton/station_reports_selected_2021_2026')
output_png = input_dir / 'pm25_seasonal_trends_with_february.png'

# Sheet candidates to try
sheet_candidates = ['Daily Aggregated', 'Filtered_Months', 'Daily', 'Sheet1']

# Load all stations data
files = sorted([p for p in input_dir.glob('*.xlsx') if p.name.endswith('.xlsx') and 'station_month_year' not in p.name.lower()])
print(f'Found {len(files)} xlsx files')

station_data = {}
for f in files:
    try:
        # Read the Excel file
        xls = pd.ExcelFile(f)
        df = None
        for sheet in sheet_candidates:
            if sheet in xls.sheet_names:
                df = pd.read_excel(xls, sheet_name=sheet)
                break
        if df is None:
            df = pd.read_excel(xls, sheet_name=0)

        # Find date column
        date_col = None
        for c in df.columns:
            if 'date' in str(c).lower() or 'timestamp' in str(c).lower() or 'time' in str(c).lower():
                date_col = c
                break
        if date_col is None:
            date_col = df.columns[0]

        # Parse dates robustly
        parsed = pd.to_datetime(df[date_col], dayfirst=True, errors='coerce')
        if parsed.isna().all():
            parsed = pd.to_datetime(df[date_col], errors='coerce')
        if parsed.isna().all():
            formats = ['%d/%m/%Y %H:%M:%S', '%d/%m/%Y', '%d-%m-%Y', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d']
            for fmt in formats:
                parsed = pd.to_datetime(df[date_col], format=fmt, errors='coerce')
                if not parsed.isna().all():
                    break
        if parsed.isna().all() and pd.api.types.is_numeric_dtype(df[date_col]):
            parsed = pd.to_datetime(df[date_col], unit='D', origin='1899-12-30', errors='coerce')
        
        df['date_parsed'] = parsed
        
        # Find PM2.5 column
        pm_cols = [c for c in df.columns if 'pm2' in str(c).lower() or 'pm_2' in str(c).lower()]
        if not pm_cols:
            print(f'  ✗ PM2.5 column not found in {f.name}')
            continue
        pm = pm_cols[0]
        df[pm] = pd.to_numeric(df[pm], errors='coerce')

        # Filter for 2021-2026 data
        mask = (df['date_parsed'] >= '2021-01-01') & (df['date_parsed'] <= '2026-12-31')
        df_filtered = df.loc[mask, ['date_parsed', pm]].copy()
        df_filtered.columns = ['date', 'pm25']
        df_filtered = df_filtered.dropna(subset=['date', 'pm25'])
        
        if df_filtered.empty:
            print(f'  ✗ No data 2021-2026 in {f.name}')
            continue

        # Extract month and year
        df_filtered['month'] = df_filtered['date'].dt.month
        df_filtered['year'] = df_filtered['date'].dt.year
        
        # Calculate monthly averages
        monthly_avg = df_filtered.groupby(['year', 'month'])['pm25'].mean().reset_index()
        monthly_avg['season_year'] = monthly_avg['year'].astype(str) + '-' + (monthly_avg['year'] + 1).astype(str)
        
        station_name = f.name.replace('.xlsx', '').replace('_', ' ')
        station_data[station_name] = monthly_avg
        print(f'  ✓ Loaded {f.name} ({len(df_filtered)} records)')
        
    except Exception as e:
        print(f'  Error reading {f.name}: {type(e).__name__}: {str(e)[:100]}')

if not station_data:
    print('No station data to plot')
    exit(1)

print(f'\nPreparing seasonal trend plot with February data...')

# Create faceted plot (all stations)
n_stations = len(station_data)
cols = 4
rows = (n_stations + cols - 1) // cols

fig, axes = plt.subplots(rows, cols, figsize=(20, 4*rows), facecolor='white')
axes = axes.flatten()

# Define months to plot (all months including February)
all_months = list(range(1, 13))
month_names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 
               'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

# Color palette for different years
years = sorted(set([y for data in station_data.values() for y in data['year'].unique()]))
colors = sns.color_palette("husl", len(years))
year_colors = {year: colors[i] for i, year in enumerate(years)}

for idx, (station_name, monthly_data) in enumerate(sorted(station_data.items())):
    ax = axes[idx]
    
    # Plot each year's trend
    for year in sorted(monthly_data['year'].unique()):
        year_data = monthly_data[monthly_data['year'] == year]
        year_data_sorted = year_data.sort_values('month')
        
        ax.plot(year_data_sorted['month'], year_data_sorted['pm25'], 
               marker='o', label=f'{year}-{year+1}', linewidth=2,
               color=year_colors[year], alpha=0.7)
    
    # Formatting
    ax.set_xlabel('Month', fontsize=10)
    ax.set_ylabel('PM2.5 (µg/m³)', fontsize=10)
    ax.set_title(station_name, fontsize=11, fontweight='bold')
    ax.set_xticks(range(1, 13))
    ax.set_xticklabels(month_names, fontsize=8, rotation=45)
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8, loc='best', ncol=2)
    ax.set_ylim(bottom=0)

# Hide unused subplots
for idx in range(n_stations, len(axes)):
    axes[idx].set_visible(False)

fig.suptitle('PM2.5 Seasonal Trends by Station (2021-2026) - Including February', 
            fontsize=14, fontweight='bold', y=0.995)
fig.tight_layout()
fig.savefig(output_png, dpi=200, bbox_inches='tight')
print(f'✓ Saved plot to {output_png}')
plt.close()

# Create a second plot: Time series with monthly aggregates (all stations combined)
print('\nCreating monthly time series plot...')

fig, ax = plt.subplots(figsize=(18, 8), facecolor='white')

for station_name, monthly_data in sorted(station_data.items()):
    # Create date column for plotting
    monthly_data_sorted = monthly_data.sort_values(['year', 'month'])
    monthly_data_sorted['date'] = pd.to_datetime(
        monthly_data_sorted['year'].astype(str) + '-' + 
        monthly_data_sorted['month'].astype(str) + '-01'
    )
    
    ax.plot(monthly_data_sorted['date'], monthly_data_sorted['pm25'], 
           marker='o', label=station_name, linewidth=1.5, alpha=0.7, markersize=3)

ax.set_xlabel('Date', fontsize=11)
ax.set_ylabel('PM2.5 (µg/m³)', fontsize=11)
ax.set_title('PM2.5 Monthly Trends - All Stations (2021-2026)', fontsize=13, fontweight='bold')
ax.grid(True, alpha=0.3)
ax.legend(fontsize=9, loc='best', ncol=2, bbox_to_anchor=(1.05, 1), framealpha=0.9)
fig.tight_layout()

timeseries_png = input_dir / 'pm25_monthly_timeseries_with_february.png'
fig.savefig(timeseries_png, dpi=200, bbox_inches='tight')
print(f'✓ Saved time series plot to {timeseries_png}')
plt.close()

# Create February-specific analysis
print('\nCreating February-focused analysis...')

fig, ax = plt.subplots(figsize=(14, 8), facecolor='white')

stations_list = []
feb_data_list = []

for station_name, monthly_data in sorted(station_data.items()):
    feb_data = monthly_data[monthly_data['month'] == 2]
    if not feb_data.empty:
        for _, row in feb_data.iterrows():
            stations_list.append(f"{station_name}\n({int(row['year'])})")
            feb_data_list.append(row['pm25'])

if feb_data_list:
    x_pos = np.arange(len(stations_list))
    bars = ax.bar(x_pos, feb_data_list, color=sns.color_palette("husl", len(stations_list)), alpha=0.7)
    
    ax.set_xlabel('Station (Year)', fontsize=11)
    ax.set_ylabel('PM2.5 (µg/m³)', fontsize=11)
    ax.set_title('February PM2.5 Levels by Station and Year', fontsize=13, fontweight='bold')
    ax.set_xticks(x_pos)
    ax.set_xticklabels(stations_list, fontsize=8, rotation=45, ha='right')
    ax.grid(True, alpha=0.3, axis='y')
    
    # Add value labels on bars
    for bar in bars:
        height = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2., height,
               f'{height:.1f}', ha='center', va='bottom', fontsize=8)
    
    fig.tight_layout()
    february_png = input_dir / 'pm25_february_analysis.png'
    fig.savefig(february_png, dpi=200, bbox_inches='tight')
    print(f'✓ Saved February analysis plot to {february_png}')
    plt.close()

print('\n✓ All plots generated successfully!')
