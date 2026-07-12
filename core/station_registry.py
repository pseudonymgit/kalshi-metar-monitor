def get_research_stations():
    """
    Returns the list of approved research stations for B-MODE backtesting.
    These are the 20 ICAO codes from the STATIC_MAPPING as required by the B-MODE specs.
    """
    static_mapping = {
        'KATL': {'city': 'Atlanta', 'state': 'GA', 'tz': 'America/New_York'},
        'KBOS': {'city': 'Boston', 'state': 'MA', 'tz': 'America/New_York'},
        'KDFW': {'city': 'Dallas-Fort Worth', 'state': 'TX', 'tz': 'America/Chicago'},
        'KDEN': {'city': 'Denver', 'state': 'CO', 'tz': 'America/Denver'},
        'KJFK': {'city': 'New York', 'state': 'NY', 'tz': 'America/New_York'},
        'KLAX': {'city': 'Los Angeles', 'state': 'CA', 'tz': 'America/Los_Angeles'},
        'KMIA': {'city': 'Miami', 'state': 'FL', 'tz': 'America/New_York'},
        'KORD': {'city': 'Chicago', 'state': 'IL', 'tz': 'America/Chicago'},
        'KSEA': {'city': 'Seattle', 'state': 'WA', 'tz': 'America/Los_Angeles'},
        'KSFO': {'city': 'San Francisco', 'state': 'CA', 'tz': 'America/Los_Angeles'},
        'KBNA': {'city': 'Nashville', 'state': 'TN', 'tz': 'America/Chicago'},
        'KHOU': {'city': 'Houston', 'state': 'TX', 'tz': 'America/Chicago'},
        'KDCA': {'city': 'Washington DC', 'state': 'VA', 'tz': 'America/New_York'},
        'KPDX': {'city': 'Portland', 'state': 'OR', 'tz': 'America/Los_Angeles'},
        'KSLC': {'city': 'Salt Lake City', 'state': 'UT', 'tz': 'America/Denver'},
        'PHNL': {'city': 'Honolulu', 'state': 'HI', 'tz': 'Pacific/Honolulu'},
        'KTPA': {'city': 'Tampa', 'state': 'FL', 'tz': 'America/New_York'},
        'KDTW': {'city': 'Detroit', 'state': 'MI', 'tz': 'America/Detroit'},
        'KCLT': {'city': 'Charlotte', 'state': 'NC', 'tz': 'America/New_York'},
        'KMSP': {'city': 'Minneapolis', 'state': 'MN', 'tz': 'America/Chicago'}
    }
    return list(static_mapping.keys())