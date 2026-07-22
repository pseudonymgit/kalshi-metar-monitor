#!/usr/bin/env python3
"""
Test script to investigate GFS GraphCast model availability in Open-Meteo
"""

import urllib.request
import json
import sys

def test_gfs_graphcast():
    """Test GFS GraphCast availability on different endpoints"""
    
    # Test 1: Generic forecast endpoint  
    print("=== Test 1: Generic forecast endpoint ===")
    ge_url = "https://api.open-meteo.com/v1/forecast?"
    ge_params = {
        "latitude": 40.7,
        "longitude": -74.0,
        "daily": "temperature_2m_max,temperature_2m_min",
        "forecast_days": 2,
        "models": "gfs_graphcast025"
    }
    ge_url += "&".join(f"{k}={v}" for k, v in ge_params.items())
    
    print(f"URL: {ge_url}")
    try:
        req = urllib.request.Request(ge_url, headers={"User-Agent": "OpenClaw-WeatherEngine/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            print(f"Response keys: {list(data.keys())}")
            
            if 'daily' in data and data['daily']:
                print(f"Daily data: {data['daily']}")
            else:
                print("No daily data found or it's empty/null")
    except Exception as e:
        print(f"Error: {e}")
        
    print()
    
    # Test 2: GFS endpoint
    print("=== Test 2: GFS specific endpoint ===")
    gfs_url = "https://api.open-meteo.com/v1/gfs?"
    gfs_params = {
        "latitude": 40.7,
        "longitude": -74.0,
        "daily": "temperature_2m_max,temperature_2m_min",
        "forecast_days": 2,
        "models": "gfs_graphcast025"
    }
    gfs_url += "&".join(f"{k}={v}" for k, v in gfs_params.items())
    
    print(f"URL: {gfs_url}")
    try:
        req = urllib.request.Request(gfs_url, headers={"User-Agent": "OpenClaw-WeatherEngine/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            print(f"Response keys: {list(data.keys())}")
            
            if 'daily' in data and data['daily'] and any(v is not None for v in (data['daily'].get('temperature_2m_max', []) or [])):
                print("SUCCESS: GFS GraphCast responded with valid data!")
                print(f"Sample temp data: {data['daily'].get('temperature_2m_max', [])}")
            else:
                print("No valid data in response or temperature data is null")
    except Exception as e:
        print(f"Error: {e}")
        
    print()

    # Test 3: GFS endpoint with forecast_days=1 (for today's possible forecasts)
    print("=== Test 3: GFS GraphCast with forecast_days=1 ===")
    gfs_url1 = "https://api.open-meteo.com/v1/gfs?"
    gfs_params1 = {
        "latitude": 40.7,
        "longitude": -74.0,
        "daily": "temperature_2m_max,temperature_2m_min",
        "forecast_days": 1,
        "models": "gfs_graphcast025"
    }
    gfs_url1 += "&".join(f"{k}={v}" for k, v in gfs_params1.items())
    
    print(f"URL: {gfs_url1}")
    try:
        req = urllib.request.Request(gfs_url1, headers={"User-Agent": "OpenClaw-WeatherEngine/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            print(f"Response keys: {list(data.keys())}")
            print(f"Daily data keys: {list(data.get('daily', {}).keys()) if 'daily' in data else 'No daily section'}")
            
            if 'daily' in data and data['daily']:
                temp_max = data['daily'].get('temperature_2m_max', [])
                print(f"Temperature max data: {temp_max}")
                if temp_max and any(v is not None for v in temp_max):
                    print("SUCCESS: Valid data returned!")
                else:
                    print("Temperature data is null or empty")
            else:
                print("No daily section in response")
    except Exception as e:
        print(f"Error: {e}")

    print()

    # Test 4: Check if the regular GFS without model parameter works (baseline)
    print("=== Test 4: Regular GFS (baseline) ===")
    baseline_url = "https://api.open-meteo.com/v1/gfs?"
    baseline_params = {
        "latitude": 40.7,
        "longitude": -74.0,
        "daily": "temperature_2m_max,temperature_2m_min",
        "forecast_days": 1
        # no models parameter, should use default
    }
    baseline_url += "&".join(f"{k}={v}" for k, v in baseline_params.items())
    
    print(f"URL: {baseline_url}")
    try:
        req = urllib.request.Request(baseline_url, headers={"User-Agent": "OpenClaw-WeatherEngine/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if 'daily' in data and data['daily'] and data['daily'].get('temperature_2m_max', []):
                print("Baseline GFS working normally!")
                print(f"Sample Baseline Data: {data['daily']['temperature_2m_max']}")
            else:
                print("Baseline GFS not providing data either")
    except Exception as e:
        print(f"Baseline Error: {e}")

def test_ecmwf_aifs():
    """Test ECMWF AIFS model (Task 3)"""
    
    print("=== Task 3: Testing ECMWF AIFS ===")
    ecmwf_url = "https://api.open-meteo.com/v1/ecmwf?"
    ecmwf_params = {
        "latitude": 40.7,
        "longitude": -74.0,
        "daily": "temperature_2m_max,temperature_2m_min",
        "forecast_days": 1,
        "models": "ecmwf_aifs025"
    }
    ecmwf_url += "&".join(f"{k}={v}" for k, v in ecmwf_params.items())
    
    print(f"URL: {ecmwf_url}")
    try:
        req = urllib.request.Request(ecmwf_url, headers={"User-Agent": "OpenClaw-WeatherEngine/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            print(f"Response keys: {list(data.keys())}")
            if 'daily' in data and data['daily']:
                print(f"Daily keys: {list(data['daily'].keys())}")
                temp_max = data['daily'].get('temperature_2m_max', [])
                if temp_max and any(v is not None for v in temp_max):
                    print("SUCCESS: ECMWF AIFS responded with valid data!")
                    print(f"Sample Temp Max: {temp_max}")
                else:
                    print("ECMWF AIFS: No valid temperature data")
            else:
                print("ECMWF AIFS: No daily section in response")
    except Exception as e:
        print(f"ECMWF AIFS Error: {e}")

def test_aifs_model_names():
    """Test various potential AIGFS model names (Task 4)"""
    
    print("=== Task 4: Testing Various AIGFS Model Names ===")
    model_names = [
        "aigfs025",
        "noaa_aigfs",
        "gfs_aigfs025", 
        "aigfs",
        "gfs_aigfs"
    ]
    
    base_url = "https://api.open-meteo.com/v1/gfs?"
    base_params = {
        "latitude": 40.7,
        "longitude": -74.0,
        "daily": "temperature_2m_max,temperature_2m_min",
        "forecast_days": 1
    }
    
    for model_name in model_names:
        print(f"\nTesting model: {model_name}")
        test_params = base_params.copy()
        test_params["models"] = model_name
        test_url = base_url + "&".join(f"{k}={v}" for k, v in test_params.items())
        
        print(f"URL: {test_url}")
        try:
            req = urllib.request.Request(test_url, headers={"User-Agent": "OpenClaw-WeatherEngine/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if 'daily' in data and data['daily']:
                    temp_vals = data['daily'].get('temperature_2m_max', [])
                    if any(v is not None for v in temp_vals if temp_vals):
                        print(f"  SUCCESS: {model_name} worked with data!")
                    else:
                        print(f"  No valid data from {model_name}")
                else:
                    print(f"  No daily section in {model_name} response")
        except Exception as e:
            print(f"  Error with {model_name}: {e}")

if __name__ == "__main__":
    print("Testing NWP Model Integrations")
    print("=" * 50)
    
    test_gfs_graphcast()
    test_ecmwf_aifs()
    test_aifs_model_names()
    
    print("\nTest summary completed.")
    
    print("\n=== Notes about GenCast Hosting (Task 5) ===")
    print("- GFS GraphCast is available through Open-Meteo's existing endpoints as graphcast models")
    print("- Meteomatics provides additional AI models but typically paid service")
    print("- Native GraphCast implementation would require significant resources (~60GB VRAM)")
    print("- The Open-Meteo version provides access to the model without hosting overhead")