#!/usr/bin/env Rscript
# PM2.5 Seasonal Trend Analysis with February Data - Base R Version
# Processes Excel files from station_reports_selected_2021_2026

library(openxlsx)

# Default input directory (can be overridden via command-line argument)
default_input_dir <- "C:/Users/Deepak iFOREST/OneDrive - iforest.global/mumbai_CPCB_staton/station_reports_selected_2021_2026"

resolve_path <- function(path_value) {
  path_value <- trimws(path_value)
  if (!nzchar(path_value)) return(path_value)
  
  normalized <- gsub("\\\\", "/", path_value)
  if (dir.exists(normalized)) {
    return(normalizePath(normalized, winslash = "/", mustWork = FALSE))
  }
  
  # Convert Windows drive paths (C:/...) to WSL mount paths (/mnt/c/...)
  if (grepl("^[A-Za-z]:/", normalized)) {
    drive_letter <- tolower(substr(normalized, 1, 1))
    path_suffix <- substring(normalized, 3)
    wsl_path <- paste0("/mnt/", drive_letter, path_suffix)
    if (dir.exists(wsl_path)) {
      return(normalizePath(wsl_path, winslash = "/", mustWork = FALSE))
    }
  }
  
  normalized
}

args <- commandArgs(trailingOnly = TRUE)
input_dir <- if (length(args) >= 1) args[1] else default_input_dir
output_dir <- if (length(args) >= 2) args[2] else input_dir

input_dir <- resolve_path(input_dir)
output_dir <- resolve_path(output_dir)

if (!dir.exists(input_dir)) {
  stop("Input directory not found: ", input_dir)
}

if (!dir.exists(output_dir)) {
  dir.create(output_dir, recursive = TRUE, showWarnings = FALSE)
}

if (!dir.exists(output_dir)) {
  stop("Output directory not found or could not be created: ", output_dir)
}

# If output dir is not writable, fallback to a local folder.
write_test_file <- tempfile(pattern = "pm25_plot_test_", tmpdir = output_dir, fileext = ".tmp")
can_write_output <- tryCatch({
  ok <- isTRUE(file.create(write_test_file))
  if (ok) unlink(write_test_file)
  ok
}, error = function(e) FALSE)

if (!can_write_output) {
  fallback_dir <- file.path(getwd(), "plot_output")
  dir.create(fallback_dir, recursive = TRUE, showWarnings = FALSE)
  cat("Output directory not writable:", output_dir, "\n")
  cat("Falling back to:", fallback_dir, "\n")
  output_dir <- fallback_dir
}

cat("Input directory:", input_dir, "\n")
cat("Output directory:", output_dir, "\n\n")

# Get all Excel files
excel_files <- list.files(input_dir, pattern = "\\.xlsx$", full.names = TRUE)
excel_files <- excel_files[!grepl("station_month_year", basename(excel_files), ignore.case = TRUE)]

cat("Found", length(excel_files), "Excel files\n\n")

# Initialize list to store data
all_data <- list()

# Function to read Excel file and extract PM2.5 data
read_station_data <- function(file_path) {
  tryCatch({
    # Get sheet names
    sheet_names <- getSheetNames(file_path)

    # Always use the first sheet
    sheet_to_read <- sheet_names[1]
    
    # Read the Excel file
    df <- read.xlsx(file_path, sheet = sheet_to_read)
    
    # Find date column
    date_col_idx <- NA
    for (i in 1:ncol(df)) {
      if (grepl(paste(c('date', 'timestamp', 'time'), collapse = '|'), 
                colnames(df)[i], ignore.case = TRUE)) {
        date_col_idx <- i
        break
      }
    }
    if (is.na(date_col_idx)) date_col_idx <- 1
    
    # Find PM2.5 column
    pm_col_idx <- NA
    for (i in 1:ncol(df)) {
      if (grepl('pm2|pm_2', colnames(df)[i], ignore.case = TRUE)) {
        pm_col_idx <- i
        break
      }
    }
    
    if (is.na(pm_col_idx)) {
      cat("  ✗ PM2.5 column not found in", basename(file_path), "\n")
      return(NULL)
    }
    
    # Extract relevant columns
    date_vals <- df[, date_col_idx]
    pm25_vals <- as.numeric(df[, pm_col_idx])
    
    # Convert date column - handle Excel numeric dates or text dates
    date_obj <- NA
    
    # First try: if numeric, treat as Excel serial
    if (is.numeric(date_vals[1])) {
      suppressWarnings({
        date_obj <- as.Date(as.numeric(date_vals), origin = "1899-12-30")
      })
    }
    
    # Second try: parse as date strings with various formats
    if (all(is.na(date_obj))) {
      suppressWarnings({
        date_obj <- as.Date(date_vals, format = "%d:%m:%Y")
      })
    }
    if (all(is.na(date_obj))) {
      suppressWarnings({
        date_obj <- as.Date(date_vals, format = "%d/%m/%Y")
      })
    }
    if (all(is.na(date_obj))) {
      suppressWarnings({
        date_obj <- as.Date(date_vals, format = "%Y-%m-%d")
      })
    }
    if (all(is.na(date_obj))) {
      suppressWarnings({
        date_obj <- as.Date(date_vals, format = "%d-%m-%Y")
      })
    }
    if (all(is.na(date_obj))) {
      suppressWarnings({
        date_obj <- as.Date(date_vals, format = "%m/%d/%Y")
      })
    }
    
    # Create data frame
    df_clean <- data.frame(
      date = date_obj,
      pm25 = pm25_vals,
      stringsAsFactors = FALSE
    )
    
    # Remove NA values
    df_clean <- df_clean[!is.na(df_clean$date) & !is.na(df_clean$pm25), ]
    
    # Filter for 2021-2026 data
    start_date <- as.Date("2021-01-01")
    end_date <- as.Date("2026-12-31")
    df_clean <- df_clean[df_clean$date >= start_date & df_clean$date <= end_date, ]
    
    if (nrow(df_clean) == 0) {
      cat("  ✗ No data 2021-2026 in", basename(file_path), "\n")
      return(NULL)
    }
    
    # Extract month and year
    df_clean$month <- as.numeric(format(df_clean$date, "%m"))
    df_clean$year <- as.numeric(format(df_clean$date, "%Y"))
    df_clean$station <- sub('\\.xlsx$', '', basename(file_path))
    df_clean$station <- gsub('_', ' ', df_clean$station)
    
    # Calculate monthly averages
    monthly_data <- list()
    years <- unique(df_clean$year)
    months <- unique(df_clean$month)
    
    for (y in sort(years)) {
      for (m in sort(months)) {
        subset_data <- df_clean[df_clean$year == y & df_clean$month == m, ]
        if (nrow(subset_data) > 0) {
          monthly_data[[paste(y, m, sep = "-")]] <- data.frame(
            year = y,
            month = m,
            station = df_clean$station[1],
            pm25_mean = mean(subset_data$pm25, na.rm = TRUE),
            stringsAsFactors = FALSE
          )
        }
      }
    }
    
    if (length(monthly_data) == 0) {
      return(NULL)
    }
    
    df_monthly <- do.call(rbind, monthly_data)
    row.names(df_monthly) <- NULL
    
    cat("  ✓ Loaded", basename(file_path), "-", nrow(df_clean), "records\n")
    return(df_monthly)
  }, 
  error = function(e) {
    cat("  Error reading", basename(file_path), "-", conditionMessage(e), "\n")
    return(NULL)
  })
}

# Load all station data
for (f in excel_files) {
  data <- read_station_data(f)
  if (!is.null(data)) {
    all_data[[length(all_data) + 1]] <- data
  }
}

if (length(all_data) == 0) {
  cat("\nNo station data to plot\n")
  quit(save = "no", status = 1)
}

# Combine all data
all_df <- do.call(rbind, all_data)
row.names(all_df) <- NULL

cat("\nData loaded successfully. Stations:", length(unique(all_df$station)), "\n")
cat("Date range:", min(all_df$year), "-", max(all_df$year), "\n")
cat("Total records:", nrow(all_df), "\n\n")

# Create faceted plot - Season trends by station (matching the attached image format)
cat("Creating seasonal trend plot matching attached image format...\n")

stations <- sort(unique(all_df$station))
n_stations <- length(stations)
n_cols <- 2
n_rows <- ceiling(n_stations / n_cols)

png(file.path(output_dir, "PM2.5_Loss_Trends_by_Season_Faceted_by_Station.png"), 
    width = 16, height = 3.5 * n_rows, units = "in", res = 200)

par(mfrow = c(n_rows, n_cols), mar = c(3.2, 3.5, 2.2, 1), oma = c(0.5, 0.5, 3, 0.5))

month_names <- c('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')

years <- sort(unique(all_df$year))

# Create season labels (year1-year2 format)
season_labels <- c()
season_colors <- c()
color_palette <- c(
  '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b'
)

year_idx <- 1
for (i in 1:(length(years)-1)) {
  season_labels <- c(season_labels, paste(years[i], years[i+1], sep="-"))
  season_colors <- c(season_colors, color_palette[((i-1) %% length(color_palette))+1])
}

for (station_idx in 1:n_stations) {
  station <- stations[station_idx]
  station_data <- all_df[all_df$station == station, ]
  
  # Get y-axis range
  y_max <- max(station_data$pm25_mean, na.rm = TRUE) * 1.15
  
  # Create plot frame
  plot(NA, xlim = c(0.5, 12.5), ylim = c(0, y_max),
       xlab = "Month", ylab = "PM2.5 (µg/m³)", 
       main = station, 
       xaxt = "n", yaxt = "n", type = "n",
       cex.main = 1.1, font.main = 1)
  
  # Add x-axis
  axis(1, at = 1:12, labels = month_names, cex.axis = 0.85, las = 1)
  
  # Add y-axis
  y_ticks <- pretty(c(0, y_max))
  axis(2, at = y_ticks, cex.axis = 0.85, las = 1)
  
  # Add grid
  grid(NA, NULL, lty = 1, col = "gray95", lwd = 0.8)
  abline(h = y_ticks, col = "white", lwd = 1)
  
  # Plot each year combination (season)
  season_idx <- 1
  for (i in 1:(length(years)-1)) {
    year1 <- years[i]
    year2 <- years[i+1]
    
    # Get data for beginning of season (year1) and end (year2)
    year1_data <- station_data[station_data$year == year1, ]
    if (nrow(year1_data) > 0) {
      year1_data <- year1_data[order(year1_data$month), ]
      lines(year1_data$month, year1_data$pm25_mean, 
            col = season_colors[season_idx], lwd = 2.2, type = "b", 
            pch = 19, cex = 0.8)
    }
    
    season_idx <- season_idx + 1
  }
}

# Main title
mtext("PM2.5 Loss Trends by Season — Faceted by Station", 
      outer = TRUE, side = 3, line = 1.2, cex = 1.4, font = 2)

# Subtitle  
mtext("Data spans Oct 2021 through Feb 2026 • Includes all months", 
      outer = TRUE, side = 3, line = 0.1, cex = 0.9, font = 1, col = "gray40")

# Add legend outside the plot area
par(xpd = TRUE)
legend(x = "bottom", horiz = TRUE, inset = c(0, -0.15),
       legend = season_labels, col = season_colors[1:length(season_labels)], 
       lwd = 2.5, pch = 19, cex = 0.9, bty = "n", 
       pt.cex = 0.9)

dev.off()
cat("✓ Saved PM2.5 seasonal trends plot\n")

# Create time series plot
cat("Creating monthly time series plot...\n")

png(file.path(output_dir, "pm25_monthly_timeseries_with_february.png"),
    width = 18, height = 8, units = "in", res = 200)

# Prepare data for time series
all_df_sorted <- all_df[order(all_df$year, all_df$month), ]
all_df_sorted$date <- as.Date(paste(all_df_sorted$year, all_df_sorted$month, "01", sep = "-"))

ylim_range <- c(0, max(all_df_sorted$pm25_mean, na.rm = TRUE) * 1.1)
xlim_range <- c(min(all_df_sorted$date), max(all_df_sorted$date))

plot(all_df_sorted$date[1], all_df_sorted$pm25_mean[1],
     xlim = xlim_range, ylim = ylim_range, type = "n",
     xlab = "Date", ylab = "PM2.5 (µg/m³)", 
     main = "PM2.5 Monthly Trends - All Stations (2021-2026)",
     xaxt = "n")

# Plot each station
station_colors <- rainbow(length(stations))
names(station_colors) <- stations

for (i in 1:length(stations)) {
  station <- stations[i]
  station_data <- all_df_sorted[all_df_sorted$station == station, ]
  lines(station_data$date, station_data$pm25_mean, 
        col = station_colors[station], lwd = 1.5, type = "b", pch = 1, cex = 0.5)
}

# Add date axis
axis.Date(1, at = seq(min(all_df_sorted$date), max(all_df_sorted$date), by = "3 months"),
          format = "%b %Y")
grid(NA, NA, lty = 2, col = "gray80")

# Add legend
legend("topright", legend = stations, col = station_colors[stations],
       lwd = 1.5, pch = 1, bty = "o", cex = 0.7, ncol = 2)

dev.off()
cat("✓ Saved monthly time series plot\n")

# February specific analysis
cat("Creating February analysis plot...\n")

feb_data <- all_df[all_df$month == 2, ]

if (nrow(feb_data) > 0) {
  feb_data$label <- paste(feb_data$station, "\n(", feb_data$year, ")", sep = "")
  feb_data <- feb_data[order(feb_data$pm25_mean, decreasing = TRUE), ]
  
  png(file.path(output_dir, "pm25_february_analysis.png"),
      width = 14, height = 8, units = "in", res = 200)
  
  par(mar = c(5, 4, 3, 2))
  
  barplot(feb_data$pm25_mean, 
          names.arg = feb_data$label,
          col = rainbow(nrow(feb_data)),
          main = "February PM2.5 Levels by Station and Year",
          xlab = "Station (Year)",
          ylab = "PM2.5 (µg/m³)",
          cex.names = 0.7,
          las = 2)
  
  # Add value labels on bars
  text(x = (1:nrow(feb_data)) * 1.2 - 0.5, 
       y = feb_data$pm25_mean + max(feb_data$pm25_mean) * 0.02,
       labels = round(feb_data$pm25_mean, 1),
       cex = 0.8, pos = 3)
  
  grid(NA, NULL, lty = 2, col = "gray80")
  
  dev.off()
  cat("✓ Saved February analysis plot\n")
}

# Summary statistics
cat("\n=== Summary Statistics ===\n")
cat("Total stations:", length(stations), "\n")
cat("Date range:", min(all_df$year), "-", max(all_df$year), "\n")
cat("Total records:", nrow(all_df), "\n\n")

# February summary
feb_summary <- all_df[all_df$month == 2, ]
if (nrow(feb_summary) > 0) {
  cat("February PM2.5 Summary (Mean across all years):\n")
  feb_stats <- aggregate(pm25_mean ~ station, data = feb_summary, 
                         FUN = function(x) c(mean = mean(x), min = min(x), max = max(x)))
  print(feb_stats)
}

cat("\n✓ All plots generated successfully!\n")
