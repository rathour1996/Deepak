import os
import pandas as pd
import glob

# Target folder with Excel files
target_folder = "/mnt/c/Users/Deepak iFOREST/OneDrive - iforest.global/mumbai_CPCB_staton/station_reports_selected_2021_2026"

# Get all Excel files
excel_files = sorted(glob.glob(os.path.join(target_folder, "*.xlsx")))

print(f"Found {len(excel_files)} Excel files\n")

for excel_file in excel_files:
    try:
        file_name = os.path.basename(excel_file)
        print(f"Processing: {file_name}")
        
        # Read the Filtered Data sheet
        df_filtered = pd.read_excel(excel_file, sheet_name='Filtered Data')
        
        print(f"  Columns before: {list(df_filtered.columns)}")
        
        # Remove unwanted columns if they exist
        cols_to_drop = [col for col in ['hours', 'coverage'] if col in df_filtered.columns]
        if cols_to_drop:
            df_filtered = df_filtered.drop(columns=cols_to_drop)
            print(f"  Dropped columns: {cols_to_drop}")
        
        # Standardize PM2.5 column name
        pm25_col = None
        for col in df_filtered.columns:
            if 'pm2' in col.lower() or 'pm_2' in col.lower():
                pm25_col = col
                break
        
        if pm25_col:
            df_filtered = df_filtered.rename(columns={pm25_col: 'PM25'})
            print(f"  Renamed '{pm25_col}' to 'PM25'")
        
        # Fix date column by converting to proper datetime
        if 'date' in df_filtered.columns:
            # Convert Excel serial dates to actual dates
            df_filtered['date'] = pd.to_datetime(df_filtered['date'], errors='coerce')
            print(f"  Fixed date column format")
        
        # Filter for years 2021-2026 in Filtered Data sheet
        if 'date' in df_filtered.columns:
            df_filtered['year'] = df_filtered['date'].dt.year
            rows_before = len(df_filtered)
            df_filtered = df_filtered[(df_filtered['year'] >= 2021) & (df_filtered['year'] <= 2026)]
            df_filtered = df_filtered.drop(columns=['year'])
            rows_after = len(df_filtered)
            print(f"  Filtered for 2021-2026: {rows_before} rows → {rows_after} rows")
        
        print(f"  Columns after: {list(df_filtered.columns)}")
        
        # Read the monthly average sheet
        df_monthly = pd.read_excel(excel_file, sheet_name='monthly average')
        
        # Rename PM2.5 column in monthly average sheet if needed
        for col in df_monthly.columns:
            if 'pm2' in col.lower() or 'pm_2' in col.lower():
                df_monthly = df_monthly.rename(columns={col: 'PM25_Average'})
                print(f"  Renamed PM2.5 column in monthly sheet to 'PM25_Average'")
                break
        
        # Filter for years 2021-2026 in monthly average sheet
        if 'Year' in df_monthly.columns:
            rows_before = len(df_monthly)
            df_monthly = df_monthly[(df_monthly['Year'] >= 2021) & (df_monthly['Year'] <= 2026)]
            rows_after = len(df_monthly)
            print(f"  Filtered monthly averages for 2021-2026: {rows_before} rows → {rows_after} rows")
        
        # Write back to Excel file
        with pd.ExcelWriter(excel_file, engine='openpyxl', mode='w') as writer:
            df_filtered.to_excel(writer, sheet_name='Filtered Data', index=False)
            df_monthly.to_excel(writer, sheet_name='monthly average', index=False)
        
        print(f"  ✓ Saved: {file_name}\n")
        
    except Exception as e:
        print(f"  ✗ Error processing {file_name}: {str(e)}\n")

print("✓ All files processed successfully!")
