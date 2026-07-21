"""
Core module for multi-stage execution system.
Implements a 3-stage order execution approach with time limits and status tracking.

Part of Phase 7 - Kalshi API Integration.
"""

import os
import sqlite3
import time
import threading
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Literal, Tuple
import uuid
import requests


def init_order_tracking_db():
    """
    Initialize SQLite database for tracking order status and stages.
    """
    db_path = os.getenv("ORDER_TRACKING_DB", "/var/data/order_tracking.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Create orders table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            station TEXT,
            market_type TEXT,
            direction TEXT,  -- 'LONG'/'SHORT'
            size REAL,
            initial_price_limit REAL,
            current_price_level REAL,
            stage INTEGER DEFAULT 1,
            stage_start_time TEXT,
            status TEXT DEFAULT 'NEW',  -- NEW, PARTIAL, FILLED, CANCELLED
            filled_qty REAL DEFAULT 0,
            total_qty REAL,
            avg_fill_price REAL DEFAULT 0,
            exchange_timestamp TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Create stage attempts table to track each stage attempt
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stage_attempts (
            attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT,
            stage INTEGER,
            limit_price REAL,
            placed_time TEXT,
            timeout_expected_at TEXT,
            qty_remaining REAL,
            filled_qty REAL,
            avg_fill_price REAL,
            status TEXT,  -- NEW, PENDING, FILLED, EXPIRED, CANCELLED
            attempt_seq INTEGER
        )
    """)
    
    # Create trigger to update updated_at
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS update_orders_updated_at 
        AFTER UPDATE ON orders 
        BEGIN 
            UPDATE orders SET updated_at = CURRENT_TIMESTAMP WHERE order_id = NEW.order_id; 
        END;
    """)
    
    conn.commit()
    conn.close()


def place_kalshi_order(
    series_ticker: str, 
    strike_price: int, 
    side: str, 
    quantity: int, 
    limit_price: float
) -> Dict[str, Any]:
    """
    Place an order on Kalshi exchange (simulated until authentication is implemented).
    Uses the order API to place limits.
    
    Args:
        series_ticker: The Kalshi series ticker
        strike_price: Strike price for the contact
        side: 'buy' or 'sell'
        quantity: Number of shares
        limit_price: Price limit (0.0 to 1.0)
        
    Returns:
        Dict with response from API or simulation
    """
    try:
        # Simulated order placement during development
        # In production, this would use authenticated API calls
        
        # Validate inputs
        if not (0.0 <= limit_price <= 1.0):
            return {"error": "Invalid price range, must be 0.0 to 1.0", "success": False}
        if side.lower() not in ["buy", "sell"]:
            return {"error": "Invalid side, must be 'buy' or 'sell'", "success": False}
        if quantity <= 0:
            return {"error": "Quantity must be positive", "success": False}
            
        # In production, these would come from proper secrets/env
        api_key = os.getenv("KALSHI_API_KEY")
        api_secret = os.getenv("KALSHI_API_SECRET")
        
        # Simulation only (would be real API in prod)
        order_result = {
            "success": True,
            "order_id": f"S_{str(uuid.uuid4())[:8]}",
            "placed_at": datetime.now().isoformat(),
            "quantity": quantity,
            "limit_price": limit_price,
            "side": side,
            "status": "pending_new",  # Until confirmed by exchange
            "simulation": True
        }
        
        # Return the simulated result
        return order_result
    except Exception as e:
        return {
            "success": False,
            "error": f"API call failed: {str(e)}"
        }


def execute_multi_stage_order(
    station: str, 
    market_type: str, 
    direction: Literal['LONG', 'SHORT'], 
    size: float
) -> Dict[str, Any]:
    """
    Execute a multi-stage order with 3 different pricing strategies:
    - Stage 1: Limit order at mid - 0.5¢, wait 30 min
    - Stage 2: If unfilled, limit order at mid price, wait 30 min
    - Stage 3: If unfilled, marketable order at best market price, deadline 90 min total
    
    Args:
        station: Station identifier like 'KJFK'
        market_type: 'HIGH' or 'LOW'
        direction: Trade direction ('LONG' or 'SHORT')
        size: Position size
        
    Returns:
        Dict with final order status
    """
    # Generate unique order ID for tracking
    order_id = f"MSE_{str(uuid.uuid4())[:12]}"
    
    # Determine series ticker from station and market type
    station_code = station[1:] if station.startswith('K') else station
    series_ticker = f"KX{market_type}{station_code}"
    
    # Get current market levels (would come from live market data)
    # Since we can't connect to real market during this session, we'll assume placeholder values
    try:
        # For now use placeholder midpoint - would come from real-time API in production
        current_midpoint = 0.45  # Placeholder midpoint
    except Exception:
        current_midpoint = 0.50  # Default if no market data
    
    # Validate inputs
    if size <= 0:
        return {"error": "Size must be positive", "success": False}
    if direction.upper() not in ["LONG", "SHORT"]:
        return {"error": "Direction must be 'LONG' or 'SHORT'", "success": False}
    if market_type.upper() not in ["HIGH", "LOW"]:
        return {"error": "Market type must be 'HIGH' or 'LOW'", "success": False}
    
    # Initial state
    init_order_tracking_db()
    total_quantity = int(size)  # Convert to integers for exchange
    filled_quantity = 0
    unfilled_quantity = total_quantity
    order_status = "CREATED"
    stage = 1
    start_time = datetime.now()
    avg_fill_price = 0.0
    
    # Calculate current strike price (for this example, assume daily market with strikes)
    from datetime import datetime
    target_date = datetime.now()
    strike_price = 80  # Placeholder strike for today - would be calculated based on forecast
    
    # Stage 1: Limit order at mid - 0.5¢
    if stage == 1:
        # Place limit order at midpoint minus 0.5 cents ($0.005)
        stage_price = current_midpoint - 0.005
        
        # For LONG order, this makes us a buyer at a price $0.005 below midpoint
        # For SHORT order, this makes us a seller at a price $0.005 below midpoint (which is counterintuitive)
        # Actually, for SHORT we should be ASKING a lower price to sell:
        if direction.upper() == 'SHORT':
            # For SHORT (selling), offer to sell at a higher price than midpoint
            stage_price = current_midpoint + 0.005
        # For LONG (buying), offer to buy at a lower price than midpoint
        # But with the spec saying "at mid-0.5¢", it likely means approach 0 or 1 depending on type
        elif direction.upper() == 'LONG':
            # For BUY (LONG), place below mid to get filled - so mid - 0.005
            stage_price = current_midpoint - 0.005
    
        # Clamp to valid Kalshi price range (0.01 - 0.99)
        stage_price = max(0.005, min(0.995, stage_price))
          
        # Attempt to place stage 1 order  
        place_result = place_kalshi_order(
            series_ticker=series_ticker,
            strike_price=strike_price,
            side="buy" if direction == "LONG" else "sell",
            quantity=unfilled_quantity,
            limit_price=stage_price
        )
        
        # Record in staging database
        record_stage_attempt(order_id, station, market_type, 1, stage_price, unfilled_quantity, "STARTED")
        
        # Simulate 30-minute wait
        time.sleep(1)  # Actual implementation would monitor API for fills
        stage_completion_time = datetime.now()
        
        # Check if order filled (simulated)
        # In real system would query Kalshi for order status
        partial_fill = False  
        fully_filled = False
        
        # For demo purposes, simulate filling based on market conditions
        stage1_simulated_fill_ratio = 0.3  # 30% fill
        stage1_fill_quantity = int(unfilled_quantity * stage1_simulated_fill_ratio)
        filled_quantity += stage1_fill_quantity
        unfilled_quantity = total_quantity - filled_quantity
        
        if filled_quantity >= total_quantity:
            fully_filled = True
            order_status = "FILLED"
            avg_fill_price = (avg_fill_price * (filled_quantity - stage1_fill_quantity) + 
                             stage_price * stage1_fill_quantity) / filled_quantity
            record_order_status(order_id, station, market_type, direction, size, 
                              stage, "FILLED", stage1_fill_quantity, total_quantity, stage_price)
        else:
            order_status = "PARTIAL_FILL"
            record_order_status(order_id, station, market_type, direction, size, 
                              stage, "PARTIAL", stage1_fill_quantity, total_quantity, stage_price)
            stage = 2
        
    
    # Stage 2: Limit order at midpoint price, wait 30 more minutes
    if stage == 2 and unfilled_quantity > 0:
        stage_price = current_midpoint
        
        # Adjust based on direction again
        if direction.upper() == 'SHORT':
            # For SHORT, place at or slightly above mid
            stage_price = current_midpoint + 0.002  # Slightly above midpoint
        elif direction.upper() == 'LONG':  
            # For LONG, place at or slightly below mid
            stage_price = current_midpoint - 0.002  # Slightly below midpoint
            
        stage_price = max(0.005, min(0.995, stage_price))
        
        place_result = place_kalshi_order(
            series_ticker=series_ticker,
            strike_price=strike_price,
            side="buy" if direction == "LONG" else "sell",
            quantity=unfilled_quantity,
            limit_price=stage_price
        )
        
        record_stage_attempt(order_id, station, market_type, 2, stage_price, unfilled_quantity, "PLACED")
        
        # Simulate another 30-min wait
        time.sleep(1)  # Real would be async monitoring
        
        # For demo purposes, simulate second fill attempt
        stage2_simulated_fill_ratio = min(0.7, unfilled_quantity / total_quantity)  # Fill remaining 70%
        stage2_fill_quantity = int(unfilled_quantity * stage2_simulated_fill_ratio)
        filled_quantity += stage2_fill_quantity
        unfilled_quantity = total_quantity - filled_quantity
        
        if filled_quantity >= total_quantity:
            fully_filled = True
            order_status = "FILLED"  
            avg_fill_price = ((avg_fill_price * (stage1_fill_quantity) + stage_price * stage2_fill_quantity) 
                             / (stage1_fill_quantity + stage2_fill_quantity))
            record_order_status(order_id, station, market_type, direction, size, 
                              stage, "FILLED", stage2_fill_quantity, total_quantity, stage_price)
        elif (datetime.now() - start_time).total_seconds() / 60 >= 90:  # Total 90 min deadline
            order_status = "CANCELLED_AT_DEADLINE"
            record_order_status(order_id, station, market_type, direction, size, 
                              stage, "CANCELLED", 0, total_quantity - filled_quantity, stage_price)
        else:
            order_status = "PARTIAL_FILL"
            stage = 3
    
    
    # Stage 3: Marketable order at best market price, deadline is 90 min
    if stage == 3 and unfilled_quantity > 0:
        # Get best market price available (would be live from API)
        # For now, simulate at current midpoint or worse
        market_price = current_midpoint
        if direction.upper() == 'LONG':
            # For buying, we accept the prevailing ask (higher price)
            market_price = min(0.995, current_midpoint + 0.01)  # Accept 1¢ worse
        elif direction.upper() == 'SHORT':
            # For selling, we accept prevailing bid (lower price)
            market_price = max(0.005, current_midpoint - 0.01)  # Accept 1¢ worse
            
        place_result = place_kalshi_order(
            series_ticker=series_ticker,
            strike_price=strike_price,
            side="buy" if direction == "LONG" else "sell", 
            quantity=unfilled_quantity,
            limit_price=market_price  # Use market-acceptable price
        )
        
        record_stage_attempt(order_id, station, market_type, 3, market_price, unfilled_quantity, "MARKET_ORDER")
        
        # Simulate last minute to deadline if not already at deadline
        time.sleep(1)
        
        # Final fill amount
        stage3_fill_quantity = unfilled_quantity
        filled_quantity += stage3_fill_quantity
        
        if filled_quantity >= total_quantity:
            order_status = "FILLED_AT_MARKET"
        else:
            order_status = "PARTIAL_AFTER_DEADLINE"
            
        record_order_status(order_id, station, market_type, direction, size,
                          stage, order_status, stage3_fill_quantity, total_quantity, market_price)
    
    # Compile final result
    result = {
        'order_id': order_id,
        'station': station,
        'market_type': market_type,
        'direction': direction,
        'requested_size': size,
        'executed_size': filled_quantity,
        'remaining_size': total_quantity - filled_quantity,
        'final_status': order_status,
        'avg_fill_price': avg_fill_price if avg_fill_price > 0 else (current_midpoint if filled_quantity > 0 else None),
        'total_execution_time_minutes': (datetime.now() - start_time).total_seconds() / 60,
        'stage_progress': stage,
        'order_sequence': [
            {'stage': i, 'status': get_stage_status(order_id, i)} 
            for i in range(1, 4)
        ]
    }
    
    # Record final order status in primary table
    finalize_order_record(order_id, order_status, filled_quantity, unfilled_quantity > 0)
    
    return result


def record_stage_attempt(order_id: str, station: str, market_type: str, stage: int, 
                         limit_price: float, quantity: int, status: str):
    """
    Record an attempt for a specific stage of the multi-stage order.
    """
    db_path = os.getenv("ORDER_TRACKING_DB", "/var/data/order_tracking.db")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute("""
        INSERT INTO stage_attempts 
        (order_id, stage, limit_price, placed_time, qty_remaining, filled_qty, avg_fill_price, 
         status, attempt_seq)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        order_id, stage, limit_price, datetime.now().isoformat(), 
        quantity, 0, 0.0, status, stage
    ))
    
    conn.commit()
    conn.close()


def get_stage_status(order_id: str, stage_num: int) -> str:
    """
    Get status of a specific stage for an order.
    """
    db_path = os.getenv("ORDER_TRACKING_DB", "/var/data/order_tracking.db")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT status FROM stage_attempts 
        WHERE order_id = ? AND stage = ? 
        ORDER BY attempt_seq DESC LIMIT 1
    """, (order_id, stage_num))
    
    result = cursor.fetchone()
    conn.close()
    
    return result[0] if result else "NOT_STARTED"


def record_order_status(order_id: str, station: str, market_type: str, direction: str,
                       size: float, stage: int, status: str, filled_qty: int,
                       total_qty: int, current_price: float):
    """
    Record current status of an active order.
    """
    db_path = os.getenv("ORDER_TRACKING_DB", "/var/data/order_tracking.db")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute("""
        INSERT OR REPLACE INTO orders
        (order_id, station, market_type, direction, size, stage, status, 
         filled_qty, total_qty, current_price_level, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
    """, (
        order_id, station, market_type, direction, size, stage, status,
        filled_qty, total_qty, current_price
    ))
    
    conn.commit()
    conn.close()


def finalize_order_record(order_id: str, final_status: str, filled_qty: int, had_remainder: bool):
    """
    Finalize an order record upon completion.
    """
    db_path = os.getenv("ORDER_TRACKING_DB", "/var/data/order_tracking.db")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Update final status for top-level order record
    cursor.execute("""
        UPDATE orders
        SET status = ?, filled_qty = ?
        WHERE order_id = ?
    """, (final_status, filled_qty, order_id))
    
    conn.commit() 
    conn.close()


def get_active_orders() -> list:
    """
    Get all active orders that are not yet completely filled or cancelled.
    """
    db_path = os.getenv("ORDER_TRACKING_DB", "/var/data/order_tracking.db")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT order_id, station, market_type, direction, size,
               stage, status, filled_qty, total_qty, created_at
        FROM orders 
        WHERE status IN ('NEW', 'PARTIAL', 'PENDING', 'ACTIVE')
              OR (filled_qty < total_qty AND status NOT LIKE '%FILLED%')
    """)
    
    active_orders = []
    columns = [col[0] for col in cursor.description]
    
    for row in cursor.fetchall():
        order_dict = {}
        for i, col in enumerate(columns):
            order_dict[col] = row[i]
        active_orders.append(order_dict)
    
    conn.close()
    return active_orders


def cancel_order_if_unfilled(order_id: str) -> Dict[str, Any]:
    """
    Cancel an order if still partially unfilled, marking the remaining balance as cancelled.
    """
    db_path = os.getenv("ORDER_TRACKING_DB", "/var/data/order_tracking.db")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Check current order status
    cursor.execute("SELECT filled_qty, total_qty, status FROM orders WHERE order_id = ?", (order_id,))
    result = cursor.fetchone()
    
    if not result:
        return {"success": False, "error": "Order not found"}
    
    filled_qty, total_qty, status = result
    
    if status in ['FILLED', 'CANCELLED', 'REJECTED']:
        return {"success": False, "error": f"Order already in status: {status}"}
    
    # Update to cancelled status but preserve partial fills
    cursor.execute("""
        UPDATE orders 
        SET status = 'CANCELLED', 
            updated_at = CURRENT_TIMESTAMP
        WHERE order_id = ?
    """, (order_id,))
    
    conn.commit()
    
    return {
        "success": True,
        "order_id": order_id, 
        "cancelled_qty": total_qty - filled_qty,
        "filled_before_cancel": filled_qty,
        "previous_status": status,
        "action": f"Order ID {order_id} was cancelled; {total_qty - filled_qty} remaining units not filled"
    }


def reset_order_tracking_db():
    """
    Utility function to recreate the database from scratch (for dev resets).
    NOTE: This destroys all existing order records!
    """
    db_path = os.getenv("ORDER_TRACKING_DB", "/var/data/order_tracking.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    init_order_tracking_db()