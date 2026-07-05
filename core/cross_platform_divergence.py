#!/usr/bin/env python3
"""
CROSS-PLATFORM PRICING DIVERGENCE TRACKER (CPDT v1.0)
Detect pricing inefficiencies between Kalshi and Polymarket for identical weather events.
Track when one platform is pricing the same event differently than the other,
and quantify potential arbitrage opportunities or market inefficiencies.

Core Function:
1. Cross-reference equivalent weather contracts across platforms
2. Identify meaningful pricing divergences (with confidence threshold) 
3. Track drift and timing of convergences/divergences  
4. Flag opportunities for exploitation

⚠️ NOTE: This is designed assuming access to both Kalshi and Polymarket APIs
  In current setup with limited data, we'll simulate this functionality.
"""

import time
import requests
import statistics
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional
import sqlite3
import json
from dataclasses import dataclass
import threading
import queue
import logging


@dataclass
class MarketEvent:
    """Represents a market event (e.g., temperature at specific location on a date)"""
    event_id: str
    platform: str  # 'kalshi', 'polymarket', etc.
    title: str 
    location: str  # City, airport code, or lat,long
    date: str  # YYYY-MM-DD
    expiration_date: str
    current_price: float  # Price as percentage (0.00-1.00)
    volume: float
    liquidity: float
    fee_rate: float
    timestamp: datetime
    metadata: dict = None  # Additional platform-specific data


@dataclass
class CrossPlatformPair:
    """Represents the same underlying event trading on both platforms"""
    kalshi_event: Optional[MarketEvent]
    polymarket_event: Optional[MarketEvent]
    correlation_score: float  # 0.0-1.0, how highly correlated these events are
    created_at: datetime = None
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()
    
    def get_divergence(self) -> Tuple[float, bool]:
        """Calculate price divergence between the two platforms 
        Returns (divergence_amount, is_meaningful)"""
        if not self.kalshi_event or not self.polymarket_event:
            return 0.0, False
        
        kalshi_price = self.kalshi_event.current_price
        pm_price = self.polymarket_event.current_price
        
        # Calculate absolute difference
        divergence = abs(kalshi_price - pm_price)
        
        is_meaningful = divergence > 0.05   # More than 5% difference considered meaningful for weather events
        
        return divergence, is_meaningful


class MarketAPIClient:
    """
    Base client class for market API interaction.
    Subclasses should implement actual market access methods.
    """
    
    def __init__(self, base_url: str, api_key: str = None):
        self.base_url = base_url
        self.api_key = api_key
        self.session = requests.Session()
        self.logger = logging.getLogger(self.__class__.__name__)
        
        # Set up session with API key if provided
        if api_key:
            self.session.headers.update({"Authorization": f"Bearer {api_key}"})
    
    def get_markets(self, category: str = "weather", date_after: str = None) -> List[MarketEvent]:
        """Get weather-related markets from the platform"""
        raise NotImplementedError("Must implement in subclass")
    
    def get_specific_market(self, market_id: str) -> Optional[MarketEvent]:
        """Get a specific market by ID"""
        raise NotImplementedError("Must implement in subclass")
    
    def get_market_history(self, market_id: str, since: datetime) -> List[Dict]:
        """Get price history for a market"""
        raise NotImplementedError("Must implement in subclass")


class MockKalshiClient(MarketAPIClient):
    """Mock implementation of Kalshi API client for demonstration"""
    
    def __init__(self):
        # Mock data with some synthetic price variations
        self.simulated_data = {
            f"KTMP-{city}-{date}": {
                'event_id': f"KTMP-{city}-{date}",
                'title': f"Will {city} hit 80°F or higher on {date}? (HIGH)",
                'location': city,
                'date': date, 
                'expiration_date': date,
                'current_price': self._simulate_base_price(city, date),
                'volume': round(1000 + (hash(f'{city}-{date}') % 10000), 2),
                'liquidity': round(10000 + (hash(f'{city}-{date}') % 50000), 2),
                'fee_rate': 0.10,    # 10%
                'timestamp': datetime.now()
            }
            for city in ['ATLANTA', 'BOSTON', 'CHICAGO', 'DENVER', 'MIAMI', 'DALLAS', 'LOS_ANGELES']
            for date in [(datetime.now() + timedelta(days=d)).strftime('%Y-%m-%d') for d in [1, 2, 3, 4, 5]]
        }
    
    def _simulate_base_price(self, city, date_str):
        """Generate realistic base price based on city and date (seasonal factors)"""
        # Generate a somewhat realistic base price based on location season
        city_season_factors = {
            'ATLANTA': 0.65, 'BOSTON': 0.40, 'CHICAGO': 0.45, 'DENVER': 0.50,
            'MIAMI': 0.85, 'DALLAS': 0.70, 'LOS_ANGELES': 0.60
        }
        
        # Seasonal adjustment
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        month = dt.month
        
        # Warmer months drive price higher (more likely to exceed temp threshold)
        if 5 <= month <= 9:  # Summer months  
            seasonal_adj = 0.05
        elif month in [3, 4, 10, 11]:  # Spring/fall
            seasonal_adj = 0.0
        else:  # Winter
            seasonal_adj = -0.15
        
        base_price = city_season_factors.get(city, 0.55) + seasonal_adj
        # Add some noise to make it realistic
        from random import uniform, seed
        seed(hash(f"{city}{date_str}") % 100000)
        noise = uniform(-0.05, 0.05)
        return max(0.01, min(0.99, base_price + noise))
    
    def get_markets(self, category="weather", date_after=None) -> List[MarketEvent]:
        # Filter mock data based on params and add some realistic volatility
        events = []
        for market_id, data in self.simulated_data.items():
            if date_after:
                if datetime.strptime(data['date'], '%Y-%m-%d') < datetime.strptime(date_after, '%Y-%m-%d'):
                    continue
            
            # Apply simulated price fluctuation for this tick
            import random
            fluctuation = random.uniform(-0.03, 0.03)  # 3% possible daily movement
            new_price = max(0.01, min(0.99, data['current_price'] + fluctuation))
            
            event = MarketEvent(
                event_id=data['event_id'],
                platform='kalshi',
                title=data['title'], 
                location=data['location'],
                date=data['date'],
                expiration_date=data['expiration_date'], 
                current_price=new_price,  # Fluctuating
                volume=data['volume'],
                liquidity=data['liquidity'],
                fee_rate=data['fee_rate'],
                timestamp=datetime.now(),
                metadata={'original_price': data['current_price']}
            )
            events.append(event)
        
        return events
    
    def get_specific_market(self, market_id: str) -> Optional[MarketEvent]:
        """Retrieve specific market data"""
        if market_id in self.simulated_data:
            data = self.simulated_data[market_id]
            return MarketEvent(
                event_id=data['event_id'],
                platform='kalshi',
                title=data['title'],
                location=data['location'], 
                date=data['date'],
                expiration_date=data['expiration_date'], 
                current_price=self._simulate_base_price(data['location'], data['date']),
                volume=data['volume'],
                liquidity=data['liquidity'],
                fee_rate=data['fee_rate'],
                timestamp=datetime.now(),
                metadata={'original_price': data['current_price']}
            )
        return None
    
    def get_market_history(self, market_id: str, since: datetime) -> List[Dict]:
        """Get simulated price history"""
        # Generate a few mock data points
        history = []
        for i in range(5):  # Last 5 data points
            dt = since + timedelta(hours=i*2)  # Every 2 hours
            import random
            fluctuation = random.uniform(-0.07, 0.07)  # Wider range in history
            price = self._simulate_base_price(self.simulated_data[market_id]['location'], self.simulated_data[market_id]['date'])
            price = max(0.01, min(0.99, price + fluctuation))
            
            history.append({
                'timestamp': dt.isoformat(),
                'price': price,
                'volume': round((random.random() + 0.5) * 100, 2)
            })
        
        return history


class MockPolymarketClient(MarketAPIClient):
    """Mock implementation of Polymarket API client for demonstration"""
    
    def __init__(self):
        # Similar mock data but with different patterns to simulate divergence
        self.simulated_data = {
            f"PTMP-{city}-{date}": {
                'event_id': f"PTMP-{city}-{date}",
                'title': f"[Polymarket] Will {city} hit 80°F or higher on {date}? (YES) - f4003",
                'location': city,
                'date': date,
                'expiration_date': date,
                'current_price': self._simulate_divergent_price(city, date),  # Different starting point from Kalshi
                'volume': round(800 + (hash(f'PM-{city}-{date}') % 8000), 2),  # Different volumes
                'liquidity': round(8000 + (hash(f'PM-{city}-{date}') % 40000), 2),
                'fee_rate': 0.08,    # Slightly different fees
                'timestamp': datetime.now()
            }
            for city in ['ATLANTA', 'BOSTON', 'CHICAGO', 'DENVER', 'MIAMI', 'DALLAS', 'LOS_ANGELES']
            for date in [(datetime.now() + timedelta(days=d)).strftime('%Y-%m-%d') for d in [1, 2, 3, 4, 5]]
        }
    
    def _simulate_divergent_price(self, city, date_str):
        """Simulate a price that differs from Kalshi, introducing potential divergence"""
        base_client = MockKalshiClient()
        kalshi_base = base_client._simulate_base_price(city, date_str)
        
        # Introduce a systematic divergence pattern
        from random import uniform
        divergence = uniform(-0.07, 0.07)  # Up to 7% potential divergence
        
        # Also vary based on platform preferences/sentiment
        platform_bias = hash(f"pm-{city}") % 100 / 1000  # Small additional bias
        new_price = kalshi_base + divergence + platform_bias
        
        return max(0.02, min(0.98, new_price))
    
    def get_markets(self, category="weather", date_after=None) -> List[MarketEvent]:
        events = []
        for market_id, data in self.simulated_data.items():
            if date_after:
                if datetime.strptime(data['date'], '%Y-%m-%d') < datetime.strptime(date_after, '%Y-%m-%d'):
                    continue
            
            # Apply daily fluctuations similar to Kalshi but different
            import random
            # Start with base but then add polymarket-specific movements
            base_price = self._simulate_divergent_price(data['location'], data['date'])
            fluctuation = random.uniform(-0.04, 0.04)  # Slightly higher variation
            new_price = max(0.02, min(0.98, base_price + fluctuation))
            
            event = MarketEvent(
                event_id=data['event_id'],
                platform='polymarket',
                title=data['title'],
                location=data['location'],
                date=data['date'], 
                expiration_date=data['expiration_date'],
                current_price=new_price,
                volume=data['volume'],
                liquidity=data['liquidity'],
                fee_rate=data['fee_rate'],
                timestamp=datetime.now(),
                metadata={'original_price': data['current_price']}
            )
            events.append(event)
        
        return events
    
    def get_specific_market(self, market_id: str) -> Optional[MarketEvent]:
        if market_id in self.simulated_data:
            data = self.simulated_data[market_id]
            return MarketEvent(
                event_id=data['event_id'], 
                platform='polymarket',
                title=data['title'],
                location=data['location'],
                date=data['date'],
                expiration_date=data['expiration_date'],
                current_price=self._simulate_divergent_price(data['location'], data['date']),
                volume=data['volume'],
                liquidity=data['liquidity'], 
                fee_rate=data['fee_rate'],
                timestamp=datetime.now(),
                metadata={'original_price': data['current_price']}
            )
        return None
    
    def get_market_history(self, market_id: str, since: datetime) -> List[Dict]:
        # Simulate Polymarket price history with its own patterns
        history = []
        for i in range(5):
            dt = since + timedelta(hours=i*2 + 1)  # Offset timing slightly to make it more realistic
            import random
            base = self._simulate_divergent_price(self.simulated_data[market_id]['location'], self.simulated_data[market_id]['date'])
            fluctuation = random.uniform(-0.05, 0.05)
            price = max(0.02, min(0.98, base + fluctuation))
            
            history.append({
                'timestamp': dt.isoformat(),
                'price': price,
                'volume': round((random.random() + 0.6) * 80, 2)  # Different volumes
            })
        
        return history


class CrossPlatformDivergenceTracker:
    """
    Main divergence tracking system. Matches equivalent events and tracks disparities.
    """
    
    def __init__(self, kalshi_client=None, polymarket_client=None, min_correlation=0.8):
        self.kalshi_client = kalshi_client or MockKalshiClient()
        self.polymarket_client = polymarket_client or MockPolymarketClient()
        self.min_correlation = min_correlation
        self.logger = logging.getLogger(self.__class__.__name__)
        self.divergence_db = self._init_db()  # SQLite for storing historical divergences
        
        # Cache recent mappings for efficiency
        self._event_cache = {}
        self.cache_ttl = timedelta(minutes=5)  # Cache for 5 minutes
        
        # Statistics tracking
        self._stats = {
            'total_comparisons': 0,
            'divergences_found': 0,
            'opportunities_flagged': 0,
            'avg_divergence': 0.0
        }
    
    def _init_db(self):
        """Initialize SQLite database for tracking historical divergences."""
        conn = sqlite3.connect('/tmp/cross_platform_divergence.db')  # Temp location; in prod would use proper path
        cursor = conn.cursor()
        
        # Table for tracking historical divergences
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS divergence_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_pair_id TEXT NOT NULL,
                kalshi_price REAL,
                polymarket_price REAL,
                divergence REAL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                significance_score REAL  -- How significant this divergence is considered
            )
        ''')
        
        # Table for flagged opportunities
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS divergence_opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_pair_id TEXT NOT NULL,
                kalshi_event_id TEXT,
               	pm_event_id TEXT,
                kalshi_price REAL,
                pm_price REAL,
                divergence REAL,
                opportunity_type TEXT, -- 'arbitrage', 'sentiment', 'inefficiency'
                confidence REAL,
                flagged_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'detected'
            )
        ''')
        
        conn.commit()
        # Connection will be closed by caller methods as needed
        return conn
    
    def match_events(self, kalshi_events: List[MarketEvent], polymarket_events: List[MarketEvent]) -> List[CrossPlatformPair]:
        """
        Match equivalent events between platforms based on location and date.
        """
        pairs = []
        
        # Create mapping to match events
        kalshi_map = {}
        for kevent in kalshi_events:
            # Use date + location as basic matching key
            key = (kevent.location.lower().replace(" ", "_"), kevent.date)
            kalshi_map[key] = kevent
        
        pm_map = {}
        for pmevent in polymarket_events:
            # Normalize and create matching key - similar approach
            loc_normal = pmevent.location.lower().replace(" ", "_")
            key = (loc_normal, pmevent.date)
            pm_map[key] = pmevent
            
        # Create pairs based on shared keys
        common_keys = set(kalshi_map.keys()) & set(pm_map.keys())
        
        for key in common_keys:
            pair = CrossPlatformPair(
                kalshi_event=kalshi_map[key],
                polymarket_event=pm_map[key],
                correlation_score=0.95  # Very confident match for matching location+date
            )
            pairs.append(pair)
        
        return pairs
    
    def detect_divergence_opportunity(self, pair: CrossPlatformPair) -> Tuple[bool, str, float]:
        """
        Determine if a meaningful and actionable divergence exists.
        
        Returns (is_opportunity, opportunity_type, confidence_score)
        """
        divergence, is_meaningful = pair.get_divergence()
        
        if not is_meaningful:
            return False, '', 0.0
        
        # Determine the nature of the opportunity
        kalshi_price = pair.kalshi_event.current_price
        pm_price = pair.polymarket_event.current_price
        
        if kalshi_price > pm_price:
            # Kalshi thinks event more likely, PM thinks less likely
            if kalshi_price > 0.8 and pm_price < 0.2:
                # Strong disagreement on high probability event - could be inefficiency  
                opportunity_type = "inefficiency"
                confidence = 0.9
            elif kalshi_price > pm_price + 0.15:
                # 15+% spread - potentially arbitrage or sentiment
                opportunity_type = "arbitrage" if (kalshi_price < 0.8 and pm_price > 0.2) else "sentiment"
                confidence = 0.75
            elif kalshi_price > pm_price + 0.1:
                # Medium spread - less confidence in trade
                opportunity_type = "inefficiency"
                confidence = 0.65
            else:  # Small spread
                opportunity_type = "sentiment"
                confidence = 0.5 if abs(divergence) > 0.07 else 0.4
        else:  # PM_price > Kalshi_price
            if pm_price > 0.8 and kalshi_price < 0.2:
                opportunity_type = "inefficiency"
                confidence = 0.9
            elif pm_price > kalshi_price + 0.15:
                opportunity_type = "arbitrage" if (pm_price < 0.8 and kalshi_price > 0.2) else "sentiment" 
                confidence = 0.75
            elif pm_price > kalshi_price + 0.1:
                opportunity_type = "inefficiency"
                confidence = 0.65
            else:
                opportunity_type = "sentiment"
                confidence = 0.5 if abs(divergence) > 0.07 else 0.4
        
        # Apply additional confidence factors:
        # If both platforms have high volume, this may indicate genuine disagreement of significance
        vol_disparity = abs(pair.kalshi_event.volume - pair.polymarket_event.volume)
        if vol_disparity > 5000:  # Significant volume difference
            # Discrepancy with high volume differential suggests a real view difference, boost confidence
            confidence = min(0.95, confidence * 1.2)  
        
        # Time factor - if approaching expiry and prices still differ markedly, confidence decreases
        exp_date = datetime.strptime(pair.kalshi_event.date, '%Y-%m-%d')  # Same for both by design
        days_to_expiry = (exp_date - datetime.now()).days
        if days_to_expiry < 2 and divergence > 0.1:
            # Large divergences close to expiry are probably going to converge naturally, reduce confidence
            confidence *= 0.7 
        elif days_to_expiry > 7 and divergence < 0.05:
            # Persistent slight divergence might just be fee differences, reduce confidence
            confidence *= 0.8
        
        return True, opportunity_type, min(1.0, confidence)
    
    def find_all_divergences(self) -> List[Dict]:
        """
        Comprehensive scan for all meaningful divergences in current market data.
        """
        # Get fresh data from both platforms
        kalshi_events = self.kalshi_client.get_markets(category="weather")
        polymarket_events = self.polymarket_client.get_markets(category="weather")
        
        # Pair up matching events
        pairs = self.match_events(kalshi_events, polymarket_events)
        
        opportunities = []
        
        for pair in pairs:
            self._stats['total_comparisons'] += 1
            
            divergence, is_meaningful = pair.get_divergence()
            self._stats['avg_divergence'] = (self._stats['avg_divergence'] * (self._stats['total_comparisons']-1) + divergence) / self._stats['total_comparisons']
            
            if is_meaningful:
                self._stats['divergences_found'] += 1
                
                is_opp, opp_type, conf = self.detect_divergence_opportunity(pair)
                
                if is_opp:
                    self._stats['opportunities_flagged'] += 1
                    
                    # Save opportunity to database
                    cur = self.divergence_db.cursor()
                    cur.execute(
                        "INSERT INTO divergence_opportunities "
                        "(event_pair_id, kalshi_event_id, pm_event_id, kalshi_price, pm_price, divergence, opportunity_type, confidence) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            f"{pair.kalshi_event.event_id}_{pair.polymarket_event.event_id}",
                            pair.kalshi_event.event_id,
                            pair.polymarket_event.event_id, 
                            pair.kalshi_event.current_price,
                            pair.polymarket_event.current_price,
                            divergence,
                            opp_type,
                            conf
                        )
                    )
                    
                    opportunities.append({
                        'kalshi_event': pair.kalshi_event.title,
                        'polymarket_event': pair.polymarket_event.title,
                        'location': pair.kalshi_event.location,
                        'date': pair.kalshi_event.date,
                        'kalshi_price': pair.kalshi_event.current_price,
                        'pm_price': pair.polymarket_event.current_price,
                        'divergence': divergence,
                        'opportunity_type': opp_type,
                        'confidence': conf
                    })
                
                # Also save to general history even if no explicit opportunity
                cur.execute(
                    "INSERT INTO divergence_history "
                    "(event_pair_id, kalshi_price, polymarket_price, divergence, significance_score) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        f"{pair.kalshi_event.event_id}_{pair.polymarket_event.event_id}",
                        pair.kalshi_event.current_price,
                        pair.polymarket_event.current_price,
                        divergence,
                        0.6 if is_meaningful else 0.2
                    )
                )
        
        self.divergence_db.commit()
        return opportunities
    
    def get_divergence_history(self, days_back=7) -> List[Dict]:
        """Get historical divergence data."""
        from_date = (datetime.now() - timedelta(days=days_back)).isoformat()
        
        cur = self.divergence_db.cursor()
        cur.execute(
            "SELECT timestamp, kalshi_price, polymarket_price, divergence "
            "FROM divergence_history WHERE timestamp > ? "
            "ORDER BY timestamp DESC",
            (from_date,)
        )
        
        results = []
        for row in cur.fetchall():
            results.append({
                'timestamp': row[0],
                'kalshi_price': row[1],
                'pm_price': row[2],
                'divergence': row[3]
            })
        
        return results
    
    def get_current_opportunities(self) -> List[Dict]:
        """Get all currently flagged opportunities."""
        cur = self.divergence_db.cursor()
        cur.execute(
            "SELECT kalshi_event_id, pm_event_id, kalshi_price, pm_price, divergence, opportunity_type, confidence "
            "FROM divergence_opportunities WHERE status = 'detected' "
            "ORDER BY confidence DESC, divergence DESC"
        )
        
        results = []
        for row in cur.fetchall():
            # We'd want to enrich with readable titles but don't store those in DB for optimization
            results.append({
                'kalshi_id': row[0], 
                'pm_id': row[1],
                'kalshi_price': row[2], 
                'pm_price': row[3],
                'divergence': row[4],
                'opportunity_type': row[5],
                'confidence': row[6]
            })
        
        return results
    
    def run_continuous_scan(self, interval_secs=300):  # 5 minutes default
        """
        Run continuous scanning for divergences (meant to run in background thread).
        """
        print(f"Starting cross-platform divergence scan (interval: {interval_secs}s)")
        
        while True:
            try:
                opportunities = self.find_all_divergences()
                
                if opportunities:
                    print(f"\n🚨 Divergence opportunities detected: {len(opportunities)}")
                    for opp in opportunities:
                        print(f"  📍 {opp['location']} - {opp['date']}")
                        print(f"     Kalshi: {opp['kalshi_price']:.3f} | PM: {opp['pm_price']:.3f} | Diff: {opp['divergence']:.3f}")
                        print(f"     Type: {opp['opportunity_type']} | Confidence: {opp['confidence']:.2f}")
                else:
                    print(f"✓ No meaningful divergences detected at {datetime.now().strftime('%H:%M:%S UTC')}")
                
                print(f"Stats: Total: {self._stats['total_comparisons']}, Divergences: {self._stats['divergences_found']}, Opportunities: {self._stats['opportunities_flagged']}")
                
                time.sleep(interval_secs)
                
            except KeyboardInterrupt:
                print("\n🛑 Divergence tracker stopped by user")
                break
            except Exception as e:
                print(f"❌ Error in scan cycle: {e}")
                time.sleep(30)  # Longer delay on errors


def main():
    """
    Demo/run Cross-Platform Divergence Tracker
    """
    print("Cross-Platform Pricing Divergence Tracker (CPDT v1.0)")
    print("=" * 80)
    print("Monitoring for Kalshi vs Polymarket pricing inconsistencies")
    
    tracker = CrossPlatformDivergenceTracker()
    
    # Perform a single scan
    print("\nRunning initial divergence scan...")
    opportunities = tracker.find_all_divergences()
    
    if opportunities:
        print(f"\n🔍 Found {len(opportunities)} potential divergence opportunities:")
        for i, opp in enumerate(opportunities, 1):
            print(f"{i:2d}. {opp['location']} ({opp['date']})")
            print(f"    Kalshi: {opp['kalshi_price']:.3f} | PM: {opp['pm_price']:.3f} | Div: {opp['divergence']:.3f}")
            print(f"    Type: {opp['opportunity_type']} | Confidence: {opp['confidence']:.2f}")
    else:
        print("\n✅ No meaningful divergences found in current scan")
    
    print("\nScanning statistics:")
    print(f"  Total comparisons: {tracker._stats['total_comparisons']}")
    print(f"  Divergences found: {tracker._stats['divergences_found']}")
    print(f"  Opportunities flagged: {tracker._stats['opportunities_flagged']}")
    print(f"  Average divergence: {tracker._stats['avg_divergence']:.3f}")
    
    # Show a bit of history
    history = tracker.get_divergence_history(days_back=1)
    if history:
        print(f"\n📈 Recent divergence history (last 24h, top 5):")
        for h in history[:5]:
            print(f"  {h['timestamp']} | K: {h['kalshi_price']:.3f} | PM: {h['pm_price']:.3f} | Δ: {h['divergence']:.3f}")
    
    print("\n🎯 Use tracker.find_all_divergences() for fresh scans")
    print("🔄 Live tracking available with tracker.run_continuous_scan()")


if __name__ == "__main__":
    main()