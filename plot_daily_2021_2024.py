#!/usr/bin/env python3
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import os
from pathlib import Path

plt.style.use('ggplot')

input_dir = Path('/mnt/c/Users/Deepak iFOREST/OneDrive - iforest.global/mumbai_CPCB_staton/station_reports_selected_2021_2026')
output_png = input_dir / 'daily_pm25_2021_2024.png'

# candidate sheets to try
sheet_candidates = ['Daily Aggregated','Filtered_Months','Sheet1','Daily']

files = sorted([p for p in input_dir.glob('*.xlsx') if p.name.endswith('.xlsx') and 'station_month_year' not in p.name.lower()])
print('Found', len(files), 'xlsx files')

series_dict = {}
for f in files:
    try:
        # try to read a sheet that exists
        df = None
        xls = pd.ExcelFile(f)
        for s in sheet_candidates:
            if s in xls.sheet_names:
                df = pd.read_excel(xls, sheet_name=s)
                break
        if df is None:
            # fallback to first sheet
            df = pd.read_excel(xls, sheet_name=0)

        # find date column
        date_col = None
        for c in df.columns:
            if 'date' in str(c).lower() or 'timestamp' in str(c).lower() or 'time' in str(c).lower():
                date_col = c
                break
        if date_col is None:
            date_col = df.columns[0]

        # parse date robustly with several fallbacks
        parsed = pd.to_datetime(df[date_col], dayfirst=True, errors='coerce')
        if parsed.isna().all():
            # try ISO / default parsing
            parsed = pd.to_datetime(df[date_col], errors='coerce')
        if parsed.isna().all():
            # try common explicit formats
            formats = ['%d/%m/%Y %H:%M:%S', '%d/%m/%Y', '%d-%m-%Y', '%d:%m:%Y %H:%M:%S', '%d:%m:%Y', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d']
            for fmt in formats:
                parsed = pd.to_datetime(df[date_col], format=fmt, errors='coerce')
                if not parsed.isna().all():
                    break
        if parsed.isna().all():
            # try Excel serial (numeric) conversion
            if pd.api.types.is_numeric_dtype(df[date_col]):
                parsed = pd.to_datetime(df[date_col], unit='D', origin='1899-12-30', errors='coerce')
        df['Parsed'] = parsed
        if df['Parsed'].isna().all():
            sample_vals = df[date_col].astype(str).dropna().unique()[:5]
            print('Could not parse dates in', f.name, 'sample:', sample_vals)
        else:
            # show range for debugging when no 2021-2024 data
            try:
                dmin = df['Parsed'].min()
                dmax = df['Parsed'].max()
                # pandas Timestamp may not be JSON serializable; convert to string
                print(f.name, 'date range:', str(dmin.date()), 'to', str(dmax.date()))
            except Exception:
                pass

        # find pm2.5 column
        pm_cols = [c for c in df.columns if 'pm2' in str(c).lower() or 'pm_2' in str(c).lower()]
        if not pm_cols:
            print('PM2.5 column not found in', f.name)
            continue
        pm = pm_cols[0]
        # coerce to numeric
        df[pm] = pd.to_numeric(df[pm], errors='coerce')

        # keep 2021-01-01 to 2024-12-31
        mask = (df['Parsed'] >= '2021-01-01') & (df['Parsed'] <= '2024-12-31')
        df = df.loc[mask, ['Parsed', pm]].dropna(subset=['Parsed'])
        if df.empty:
            print('No data 2021-2024 in', f.name)
            continue

        # resample to daily mean if timestamps are finer
        df = df.set_index('Parsed')
        daily = df[pm].resample('D').mean()
        if daily.dropna().empty:
            continue

        label = f.name.replace('.xlsx','').replace('_',' ')
        series_dict[label] = daily
        print('Loaded', f.name)
    except Exception as e:
        print('Error reading', f.name, e)

if not series_dict:
    print('No series to plot')
    exit(0)

# create DataFrame with all series aligned
all_df = pd.DataFrame(series_dict)

# define plotting helper with informative x-axis (years major, months minor)
from matplotlib.dates import DateFormatter, MonthLocator, YearLocator

start = pd.to_datetime('2021-01-01')
end = pd.to_datetime('2024-12-31')

def save_plot(df_plot, out_path, title, rolling_window=30):
    if df_plot.dropna(how='all').empty:
        print('No data to plot for', out_path.name)
        return
    fig, ax = plt.subplots(figsize=(16,10), facecolor='white')
    ax.set_facecolor('white')
    # single unified line per station: plot rolling mean only (one line per station)
    rolling = df_plot.rolling(window=rolling_window, min_periods=max(1,int(rolling_window/4)), center=True).mean()
    for col in rolling.columns:
        ax.plot(rolling.index, rolling[col], lw=2.0, label=col, solid_capstyle='round')

    ax.set_xlim(start, end)
    ax.set_xlabel('Date')
    ax.set_ylabel('PM2.5 (µg/m3)')
    ax.set_title(title)

    # x-axis: show months Jan, Oct, Nov, Dec with years centered below
    months_to_show = [1, 10, 11, 12]
    ax.xaxis.set_major_locator(MonthLocator(bymonth=months_to_show))
    ax.xaxis.set_major_formatter(DateFormatter('%b'))
    ax.tick_params(axis='x', which='major', labelsize=10, rotation=90)
    
    # add a secondary axis for year labels centered at midpoint of each year (below)
    ax_year = ax.twiny()
    ax_year.set_xlim(ax.get_xlim())
    year_positions = [pd.to_datetime(f'{year}-07-01') for year in [2021, 2022, 2023, 2024]]
    ax_year.set_xticks(year_positions)
    ax_year.set_xticklabels([str(year) for year in [2021, 2022, 2023, 2024]], fontsize=10)
    ax_year.xaxis.set_label_position('bottom')
    ax_year.set_xlabel('Date', fontsize=11, labelpad=40)
    ax_year.tick_params(axis='x', labelbottom=True, labeltop=False)
    ax_year.spines['bottom'].set_position(('outward', 30))
    ax_year.spines['top'].set_visible(False)
    ax.set_xlabel('')  # remove Date label from main axis

    ax.grid(True, which='major', linestyle='--', alpha=0.4)
    # move legend inside plot area at upper right with smaller font and two columns
    ax.legend(loc='upper right', fontsize='x-small', ncol=2, framealpha=0.7)
    fig.tight_layout(pad=1.0)
    fig.savefig(out_path, dpi=200)
    plt.close(fig)
    print('Saved plot to', out_path)

# Daily
save_plot(all_df, output_png, 'Daily PM2.5: 2021-2024', rolling_window=30)

# Weekly (resample to weekly mean)
weekly = all_df.resample('W').mean()
weekly_png = input_dir / 'weekly_pm25_2021_2024.png'
save_plot(weekly, weekly_png, 'Weekly PM2.5: 2021-2024', rolling_window=4)

# Monthly (resample to monthly mean)
monthly = all_df.resample('ME').mean()
monthly_png = input_dir / 'monthly_pm25_2021_2024.png'
save_plot(monthly, monthly_png, 'Monthly PM2.5: 2021-2024', rolling_window=3)

# Weekly plot for selected 3 stations
selected_station_names = ['Deonar_Mumbai_IITM', 'Navy_Nagar-Colaba_Mumbai_IITM', 'Malad_West_Mumbai_IITM']
# labels in series_dict use spaces instead of underscores
selected_labels = [s.replace('_', ' ') for s in selected_station_names]
weekly_selected = weekly[[c for c in weekly.columns if c in selected_labels]]
weekly_selected_png = input_dir / 'weekly_pm25_selected_3stations_2021_2024.png'
save_plot(weekly_selected, weekly_selected_png, 'Weekly PM2.5: Selected Stations (3) 2021-2024', rolling_window=4)

# Weekly plot for second group (Mulund West, Sion, Kurla)
group2_station_names = ['Mulund_West_Mumbai_MPCB', 'Sion_Mumbai_MPCB', 'Kurla_Mumbai_MPCB']
group2_labels = [s.replace('_', ' ') for s in group2_station_names]
weekly_group2 = weekly[[c for c in weekly.columns if c in group2_labels]]
weekly_group2_png = input_dir / 'weekly_pm25_group2_3stations_2021_2024.png'
save_plot(weekly_group2, weekly_group2_png, 'Weekly PM2.5: Mulund, Sion, Kurla (3) 2021-2024', rolling_window=4)
# plots for group of four stations (title generic)
group3_station_names = ['Deonar_Mumbai_IITM', 'Navy_Nagar-Colaba_Mumbai_IITM',
                        'Kurla_Mumbai_MPCB', 'Malad_West_Mumbai_IITM']
group3_labels = [s.replace('_', ' ') for s in group3_station_names]
group3_df = all_df[[c for c in all_df.columns if c in group3_labels]]
if not group3_df.empty:
    # daily
    daily_group3_png = input_dir / 'daily_pm25_group4stations_2021_2024.png'
    save_plot(group3_df, daily_group3_png, 'Daily PM2.5 2021-2024', rolling_window=30)
    # weekly
    weekly_group3 = group3_df.resample('W').mean()
    weekly_group3_png = input_dir / 'weekly_pm25_group4stations_2021_2024.png'
    save_plot(weekly_group3, weekly_group3_png, 'Weekly PM2.5 2021-2024', rolling_window=4)
    # monthly
    monthly_group3 = group3_df.resample('ME').mean()
    monthly_group3_png = input_dir / 'monthly_pm25_group4stations_2021_2024.png'
    save_plot(monthly_group3, monthly_group3_png, 'Monthly PM2.5 2021-2024', rolling_window=3)
# pair plot for Borivali East and Powai
pair_station_names = ['Borivali_East_Mumbai_MPCB', 'Powai_Mumbai_MPCB']
pair_labels = [s.replace('_', ' ') for s in pair_station_names]
pair_df = all_df[[c for c in all_df.columns if c in pair_labels]]
if not pair_df.empty:
    # daily
    daily_pair_png = input_dir / 'daily_pm25_borivali_powai_2021_2024.png'
    save_plot(pair_df, daily_pair_png, 'Daily PM2.5 Borivali East & Powai 2021-2024', rolling_window=30)
    # weekly
    weekly_pair = pair_df.resample('W').mean()
    weekly_pair_png = input_dir / 'weekly_pm25_borivali_powai_2021_2024.png'
    save_plot(weekly_pair, weekly_pair_png, 'Weekly PM2.5 Borivali East & Powai 2021-2024', rolling_window=4)
    # monthly
    monthly_pair = pair_df.resample('ME').mean()
    monthly_pair_png = input_dir / 'monthly_pm25_borivali_powai_2021_2024.png'
    save_plot(monthly_pair, monthly_pair_png, 'Monthly PM2.5 Borivali East & Powai 2021-2024', rolling_window=3)
# third group of four stations
group4_station_names = ['Sion_Mumbai_MPCB', 'Chhatrapati_Shivaji_Intl._Airport_(T2)_Mumbai_MPCB',
                        'Vile_Parle_West_Mumbai_MPCB', 'Mulund_West_Mumbai_MPCB']
group4_labels = [s.replace('_', ' ') for s in group4_station_names]
group4_df = all_df[[c for c in all_df.columns if c in group4_labels]]
if not group4_df.empty:
    # daily
    daily_group4_png = input_dir / 'daily_pm25_group4b_2021_2024.png'
    save_plot(group4_df, daily_group4_png, 'Daily PM2.5 2021-2024', rolling_window=30)
    # weekly
    weekly_group4 = group4_df.resample('W').mean()
    weekly_group4_png = input_dir / 'weekly_pm25_group4b_2021_2024.png'
    save_plot(weekly_group4, weekly_group4_png, 'Weekly PM2.5 2021-2024', rolling_window=4)
    # monthly
    monthly_group4 = group4_df.resample('ME').mean()
    monthly_group4_png = input_dir / 'monthly_pm25_group4b_2021_2024.png'
    save_plot(monthly_group4, monthly_group4_png, 'Monthly PM2.5 2021-2024', rolling_window=3)