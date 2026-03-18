import os
import pandas as pd
import glob
from pathlib import Path

# Define input and output directories
input_dir = "/mnt/c/Users/Deepak iFOREST/OneDrive - iforest.global/mumbai_CPCB_staton/raw data/cpcb_daily"
output_dir = "/mnt/c/Users/Deepak iFOREST/OneDrive - iforest.global/mumbai_CPCB_staton/raw data/cpcb_daily_filtered"

# Create output directory if it doesn't exist
os.makedirs(output_dir, exist_ok=True)

# Get all CSV files
csv_files = sorted(glob.glob(os.path.join(input_dir, "*.csv")))

print(f"Found {len(csv_files)} station files")

# Target months (January, October, November, December)
target_months = [1, 10, 11, 12]

for csv_file in csv_files:
    try:
        print(f"\nProcessing: {os.path.basename(csv_file)}")
        
        # Extract station name from filename
        filename = os.path.basename(csv_file)
        station_name = filename.replace("station_", "").replace("_daily.csv", "")
        
        # Read CSV file
        df = pd.read_csv(csv_file)
        
        # Convert date column to datetime
        df['date'] = pd.to_datetime(df['date'], errors='coerce')
        
        # Standardize PM2.5 column name
        pm25_col = None
        for col in df.columns:
            if 'pm2' in col.lower() or 'pm_2' in col.lower():
                pm25_col = col
                break
        
        if pm25_col is None:
            print(f"  Warning: PM2.5 column not found in {station_name}")
            continue
        
        # Remove unwanted columns
        cols_to_drop = [col for col in ['hours', 'coverage'] if col in df.columns]
        df = df.drop(columns=cols_to_drop)
        
        # Rename PM2.5 column for consistency
        df = df.rename(columns={pm25_col: 'PM25'})
        
        # Extract month and year from date
        df['month'] = df['date'].dt.month
        df['year'] = df['date'].dt.year
        
        # Filter for target months
        filtered_df = df[df['month'].isin(target_months)].copy()
        
        if len(filtered_df) == 0:
            print(f"  No data found for target months in {station_name}")
            continue
        
        # Prepare data for export - keep only necessary columns
        filtered_export = filtered_df[['date', 'PM25']].copy()
        
        # Calculate monthly and yearly averages
        monthly_avg_data = []
        for year in sorted(filtered_df['year'].unique()):
            year_data = filtered_df[filtered_df['year'] == year]
            for month in sorted(year_data['month'].unique()):
                month_data = year_data[year_data['month'] == month]
                pm25_avg = month_data['PM25'].mean()
                
                month_names = {1: 'January', 10: 'October', 11: 'November', 12: 'December'}
                month_name = month_names.get(month, '')
                
                monthly_avg_data.append({
                    'Year': year,
                    'Month': month_name,
                    'PM25_Average': pm25_avg
                })
        
        monthly_avg = pd.DataFrame(monthly_avg_data)
        
        # Create Excel file with two sheets
        excel_filename = os.path.join(output_dir, f"{station_name}.xlsx")
        
        with pd.ExcelWriter(excel_filename, engine='openpyxl') as writer:
            # Write filtered data to first sheet
            filtered_export.to_excel(writer, sheet_name='Filtered Data', index=False)
            
            # Write monthly averages to second sheet
            monthly_avg.to_excel(writer, sheet_name='monthly average', index=False)
        
        print(f"  ✓ Created {excel_filename}")
        print(f"    - Filtered Data sheet: {len(filtered_export)} rows")
        print(f"    - Monthly Average sheet: {len(monthly_avg)} rows")
        
    except Exception as e:
        print(f"  ✗ Error processing {csv_file}: {str(e)}")

print(f"\n✓ All files processed successfully!")
print(f"Output directory: {output_dir}")
