#!/usr/bin/env python3
"""
EDGE 5: Forecast Disagreement (GFS vs NWS) — REAL API Integration

Core signal: Compare GFS 0.25° model forecasts vs NWS human forecasts.
When disagreement >5°F, bet in GFS direction.
Real API integration: GFS from NOMADS, NWS from api.weather.gov.

Note: For walk-forward BACKTEST purposes:
  - We can only collect forecast data going FORWARD (from now on)  
  - No historical NWS API archive exists publicly
  - Historical GFS data is difficult to access for 5 years of backtesting
  - Therefore REAL backtesting will be done via forward testing after data collection

This builds the full real integration but focuses on COLLECTING the data.

Walk-forward only. No AI in the loop. No proxies (except where necessary for backtesting).
"""

import sqlite3
import json
import urllib.request
import math
import os
import time
from datetime import datetime, timedelta
import ssl

# Database setup
DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"
COLLECTION_DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/forecast_disagreement_real.db"

# Station mappings - latitude/longitude for 21 Kalshi cities
STATIONS = {
    'KATL': (33.6407, -84.4277), 'KAUS': (30.1945, -97.6699),
    'KBOS': (42.3656, -71.0096), 'KDAL': (32.8471, -96.8517),
    'KDCA': (38.8512, -77.0402), 'KDEN': (39.8561, -104.6737),
    'KDFW': (32.8998, -97.0403), 'KHOU': (29.6454, -95.2789),
    'KLAS': (36.0840, -115.1537), 'KLAX': (33.9425, -118.4081),
    'KMDW': (41.7868, -87.7522), 'KMIA': (25.7959, -80.2870),
    'KMSP': (44.8848, -93.2223), 'KMSY': (29.9934, -90.2580),
    'KNYC': (40.7829, -73.9654), 'KOKC': (35.3931, -97.6007),
    'KPHL': (39.8744, -75.2424), 'KPHX': (33.4342, -112.0116),
    'KSAT': (29.5337, -98.4698), 'KSEA': (47.4502, -122.3088),
    'KSFO': (37.6213, -122.3790),
}

# Open-Meteo API for GFS data
OPEN_METEO_GFS_URL = "https://api.open-meteo.com/v1/forecast"

class ForecastDataCollector:
    def __init__(self):
        self.setup_database()
        
    def setup_database(self):
        """Setup forecast collection database."""
        conn = sqlite3.connect(COLLECTION_DB_PATH)
        cur = conn.cursor()
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS forecast_collection_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station_code TEXT,
                collection_date TEXT,
                gfs_temperature_f REAL,
                nws_temperature_f REAL,
                temperature_difference_f REAL,
                status TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS collected_forecasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station_code TEXT,
                forecast_date TEXT,  -- The date being forecast
                issued_date TEXT,    -- When the forecast was issued  
                gfs_high_f REAL,     -- GFS predicted high
                nws_high_f REAL,     -- NWS predicted high
                actual_high_f REAL,  -- Actual observed high (when available)
                status TEXT,         -- 'forecast_only', 'actual_available'
                collected_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        conn.commit()
        conn.close()
    
    def get_nws_forecast_endpoint(self, lat, lon):
        """Get the NWS gridpoint forecast endpoint for a location."""
        try:
            url = f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
            req = urllib.request.Request(url, headers={'User-Agent': 'OpenClaw-Weather-Engine/1.0'})
            with urllib.request.urlopen(req, timeout=15) as response:
                data = json.loads(response.read().decode())
                
            office_id = data['properties']['gridId']
            x = data['properties']['gridX']
            y = data['properties']['gridY']
            
            return f"https://api.weather.gov/gridpoints/{office_id}/{x},{y}/forecast"
        except Exception as e:
            print(f"Failed to get NWS endpoint for {lat}, {lon}: {e}")
            return None
    
    def get_current_nws_forecast(self, lat, lon):
        """Get current NWS forecast for a location."""
        endpoint = self.get_nws_forecast_endpoint(lat, lon)
        if not endpoint:
            return None
            
        try:
            req = urllib.request.Request(endpoint, headers={'User-Agent': 'OpenClaw-Weather-Engine/1.0'})
            with urllib.request.urlopen(req, timeout=15) as response:
                data = json.loads(response.read().decode())
                
            # Extract daily high temperatures from forecast periods
            forecasts = {}
            for period in data['properties']['periods']:
                start_time = period['startTime'][0:10]  # YYYY-MM-DD
                if period['isDaytime']:  # Only process daytime periods for daily high
                    forecast_date = start_time
                    temp = period['temperature']
                    unit = period['temperatureUnit']
                    if unit == 'F':
                        forecasts[forecast_date] = temp
                    else:  # Assume C
                        forecasts[forecast_date] = temp * 9/5 + 32
                    
            return forecasts  # dict: {date: forecast_temp_F}
        except Exception as e:
            print(f"Failed to get NWS forecast for {endpoint}: {e}")
            return None
    
    def get_current_gfs_forecast(self, lat, lon):
        """Get current GFS forecast data."""
        try:
            params = f"?latitude={lat}&longitude={lon}&daily=temperature_2m_max&timezone=auto&models=gfs_seamless"
            url = OPEN_METEO_GFS_URL + params
            
            req = urllib.request.Request(url, headers={'User-Agent': 'OpenClaw-Weather-Engine/1.0'})
            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode())
                
            daily_data = {}
            if 'daily' in data:
                dates = data['daily'].get('time', [])
                temps = data['daily'].get('temperature_2m_max', [])  # This comes in Celsius
                
                for i, date in enumerate(dates):
                    if i < len(temps) and temps[i] is not None:
                        temp_f = temps[i] * 9/5 + 32  # Convert C to F
                        daily_data[date] = temp_f
                        
            return daily_data  # dict: {date: forecast_temp_F}
        except Exception as e:
            print(f"Failed to get GFS forecast for {lat}, {lon}: {e}")
            return None
    
    def collect_daily_forecasts(self, station_code):
        """Collect GFS and NWS forecasts for a station."""
        print(f"Collecting forecasts for {station_code}...")
        
        lat, lon = STATIONS[station_code]
        
        # Get NWS forecast
        nws_data = self.get_current_nws_forecast(lat, lon)
        print(f"  NWS forecasts retrieved: {len(nws_data) if nws_data else 0}")
        
        # Get GFS forecast
        gfs_data = self.get_current_gfs_forecast(lat, lon)
        print(f"  GFS forecasts retrieved: {len(gfs_data) if gfs_data else 0}")
        
        if not nws_data or not gfs_data:
            return False
            
        # Save the forecasts
        conn = sqlite3.connect(COLLECTION_DB_PATH)
        cur = conn.cursor()
        
        collection_date = datetime.utcnow().strftime('%Y-%m-%d')
        
        # Identify common forecast dates and store them
        common_dates = set(nws_data.keys()) & set(gfs_data.keys())
        
        for date in common_dates:
            gfs_temp = gfs_data[date]
            nws_temp = nws_data[date]
            diff = abs(gfs_temp - nws_temp)
            
            # Insert into the database
            cur.execute("""
                INSERT OR REPLACE INTO collected_forecasts 
                (station_code, forecast_date, issued_date, gfs_high_f, nws_high_f, status, collected_at)
                VALUES (?, ?, ?, ?, ?, 'forecast_only', ?)
            """, (station_code, date, collection_date, gfs_temp, nws_temp, collection_date))
            
            # Insert a log entry
            cur.execute("""
                INSERT INTO forecast_collection_log
                (station_code, collection_date, gfs_temperature_f, nws_temperature_f, 
                 temperature_difference_f, status)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (station_code, date, gfs_temp, nws_temp, diff, "OK"))
        
        conn.commit()
        conn.close()
        
        print(f"  Collected {len(common_dates)} forecast pairs for {station_code}")
        return len(common_dates) > 0
    
    def collect_all_stations_daily(self):
        """Collect forecasts for all stations."""
        successful_collections = 0
        
        for station_code in STATIONS:
            try:
                success = self.collect_daily_forecasts(station_code)
                if success:
                    successful_collections += 1
                time.sleep(1)  # Be respectful to APIs
            except Exception as e:
                print(f"Error collecting {station_code}: {e}")
        
        return successful_collections
    
    def compute_forecast_disagreement_signal(self, gfs_temp, nws_temp):
        """Compute forecast disagreement signal per Gray Room spec.
        
        If |GFS_T - NWS_T| > 5°F → bet in GFS direction
        """
        disagreement_threshold = 5.0  # °F
        disagreement = abs(gfs_temp - nws_temp)
        
        if disagreement < disagreement_threshold:
            return None, 0.0  # No signal
        
        # Bet in the direction of GFS
        direction = 'up' if gfs_temp > nws_temp else 'down'
        
        # Calculate signal strength using sigmoid
        def sigmoid(x):
            return 1.0 / (1.0 + math.exp(-x))
        
        confidence = sigmoid((disagreement - disagreement_threshold) / 3.0)
        
        return direction, confidence
    
    def run_forward_test(self, min_wait_minutes=10):
        """Run forward test using collected data after accumulating some history."""
        print("="*80)
        print("FOCUS SHIFT: BACKTEST → FORWARD TEST")
        print("="*80)
        print("Historical forecast data is not available via public APIs.")
        print("Therefore, we must collect forecasts going FORWARD for real testing.")
        print(f"Waiting {min_wait_minutes} minutes between collections...")
        print("To run v10 ensemble backtest: Use proxy data for historical period")
        print("but validate methodology using this real data collection framework.")
        print("")
        print("Starting data collection:")
        
        cycle_count = 0
        while True:
            try:
                print(f"\n--- Collection Cycle {cycle_count+1} ---")
                
                count = self.collect_all_stations_daily()
                print(f"Collected forecasts for {count}/21 stations successfully")
                
                cycle_count += 1
                if count > 0:
                    print("Data collection successful. Ready for analysis when needed.")
                    break  # For now, just do one cycle as an example
                
                time.sleep(min_wait_minutes * 60)  # Convert to seconds
            except KeyboardInterrupt:
                print("\nCollection interrupted by user.")
                break
            except Exception as e:
                print(f"Error in collection cycle: {e}")
                time.sleep(min_wait_minutes * 60)


def main():
    print("="*90)
    print("EDGE 5: Forecast Disagreement (GFS vs NWS) — REAL API Integration")
    print("="*90)
    print("Database for collections: ", COLLECTION_DB_PATH)
    print("Stations: 21 Kalshi locations")
    print("APIs:")
    print("  - GFS: Open-Meteo API pulling from NOMADS (models=gfs_seamless)")  
    print("  - NWS: api.weather.gov/gridpoints endpoint (CURRENT forecasts only)")
    print("")
    print("LIMITATION: Historical NWS forecasts not available via API")
    print("SOLUTION: Collect forwards, test with accumulated data")
    print("")
    
    collector = ForecastDataCollector()
    
    print("Step 1: Testing API connectivity for one station (KLAX)...")
    
    # Test just one station as proof of concept
    lat, lon = STATIONS['KLAX']
    
    print(f"NWS endpoint: {collector.get_nws_forecast_endpoint(lat, lon)}")
    
    nws_forecasts = collector.get_current_nws_forecast(lat, lon)
    if nws_forecasts:
        print(f"NWS current forecasts fetched: {len(nws_forecasts)} days")
        
    gfs_forecasts = collector.get_current_gfs_forecast(lat, lon)
    if gfs_forecasts:
        print(f"GFS current forecasts fetched: {len(gfs_forecasts)} days")
    
    # Demonstrate signal computation if we have data
    if nws_forecasts and gfs_forecasts:
        common_dates = set(nws_forecasts.keys()) & set(gfs_forecasts.keys())
        print(f"\nAvailable forecasts for demonstration: {len(common_dates)} days")
        
        if common_dates:
            date = list(common_dates)[0]
            nws_temp = nws_forecasts[date]
            gfs_temp = gfs_forecasts[date]
            print(f"Example data - Date: {date}, NWS: {nws_temp:.1f}°F, GFS: {gfs_temp:.1f}°F")
            
            direction, confidence = collector.compute_forecast_disagreement_signal(
                gfs_temp, nws_temp)
            
            if direction:
                disagreement = abs(gfs_temp - nws_temp)
                print(f"  Signal: {direction} direction, confidence: {confidence:.3f}")
                print(f"  |difference|: {disagreement:.1f}°F (>5°F threshold)")
            else:
                disagreement = abs(gfs_temp - nws_temp)
                print(f"  No signal - difference only {disagreement:.1f}°F (<5°F threshold)")
    
    print("\nStep 2: Beginning data collection...")
    print("Will collect for all 21 stations...")
    collector.collect_all_stations_daily()
    
    print("\nStep 3: Data collection infrastructure is ready.")
    print("Real forecast disagreement data is now being collected.")
    print("For actual testing: Accumulate this data over time.")
    print("For backtesting: This demonstrates REAL API integration.")
    
    print("\n" + "="*90)
    print("EDGE 5 STATUS: READY FOR DATA COLLECTION")
    print("- Real APIs integrated: GFS (NOMADS via Open-Meteo), NWS (gridpoints)")
    print("- Collection database created: forecasts stored, retrievable")
    print("- Signal logic implemented: |diff| > 5°F → bet GFS direction")
    print("- Historical limitation acknowledged: forward testing necessary") 
    print("="*90)


if __name__ == "__main__":
    main()
