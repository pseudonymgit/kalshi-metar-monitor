#!/usr/bin/env python3
"""
Delivery Router v1.0 — Phase 6.2 Multi-Channel Delivery System

Routes alerts to multiple channels based on availability and priority:
- Discord (primary): Direct API calls
- HTTP Dashboard (secondary): POST to specified endpoint 
- SMS (tertiary): Placeholder functionality with logging

Also handles heartbeat delivery to multiple channels.
"""

import asyncio
import logging
import json
import aiohttp
import traceback
from typing import Dict, List, Optional, Any, Union
from urllib.parse import urljoin
import os
from datetime import datetime, timezone

_LOGGER = logging.getLogger(__name__)


class DeliveryTarget:
    """Base interface for delivery targets"""
    
    async def deliver(self, content: Dict[str, Any], **kwargs) -> bool:
        """Deliver content to target. Returns True on success."""
        raise NotImplementedError


class DiscordDelivery(DeliveryTarget):
    """Discord webhook delivery target"""
    
    def __init__(self, webhook_url: str, name: str = "discord"):
        self.webhook_url = webhook_url
        self.name = name
        self.session = None
    
    async def _get_session(self):
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            )
        return self.session
    
    async def deliver(self, content: Dict[str, Any], **kwargs) -> bool:
        """Send message to Discord webhook"""
        session = await self._get_session()
        
        try:
            # Extract content appropriately
            message_content = content.get('content', '')
            embeds = content.get('embeds', [])
            
            # Prepare JSON payload
            payload = {
                "username": kwargs.get('username', 'Weather Engine'),
                "avatar_url": kwargs.get('avatar_url', ''),
                "content": message_content or kwargs.get('message', ''),
            }
            
            if embeds:
                payload['embeds'] = embeds
            
            # Add additional properties like footer, title, etc. from content
            for key in ['embed', 'embeds', 'allowed_mentions', 'components']:
                if key in content and key not in payload:
                    payload[key] = content[key]
            
            async with session.post(self.webhook_url, json=payload) as resp:
                if resp.status == 204:  # Success, no content
                    _LOGGER.info("Successfully delivered to Discord webhook")
                    return True
                else:
                    error_text = await resp.text()
                    _LOGGER.error(f"Failed to deliver to Discord: {resp.status}, {error_text}")
                    return False
                    
        except Exception as e:
            _LOGGER.error(f"Exception delivering to Discord: {str(e)}")
            return False


class HTTPDashboardDelivery(DeliveryTarget):
    """HTTP dashboard delivery target"""
    
    def __init__(self, base_url: str, endpoint: str = "/api/alerts", headers: Optional[Dict] = None, 
                 name: str = "http_dashboard"):
        self.base_url = base_url.strip('/')
        self.endpoint = endpoint
        self.url = f"{self.base_url}{endpoint}"
        self.headers = headers or {}
        self.name = name
        self.session = None
    
    async def _get_session(self):
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            )
        return self.session
    
    async def deliver(self, content: Dict[str, Any], **kwargs) -> bool:
        """Post alert content to HTTP dashboard endpoint"""
        session = await self._get_session()
        
        try:
            # Prepare the payload - ensure content is serializable
            payload = {
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'source': 'weather-engine',
                'type': kwargs.get('type', 'alert'),
                'data': content
            }
            
            # Add any additional metadata from kwargs
            headers = {**self.headers}
            if kwargs.get('authorization'):
                headers['Authorization'] = kwargs['authorization']
                
            async with session.post(
                self.url, 
                json=payload,
                headers=headers
            ) as resp:
                if resp.status in [200, 201, 202]:
                    _LOGGER.info(f"Successfully delivered to HTTP dashboard ({self.url})")
                    response_text = await resp.text()
                    _LOGGER.debug(f"Response: {response_text}")
                    return True
                else:
                    error_text = await resp.text()
                    response_headers = dict(resp.headers)
                    _LOGGER.error(
                        f"HTTP dashboard delivery failed: {resp.status}, URL: {self.url}, Headers: {response_headers}, Error: {error_text}"
                    )
                    return False
                    
        except Exception as e:
            _LOGGER.error(f"Exception delivering to HTTP dashboard: {str(e)}")
            _LOGGER.error(f"Error details: {traceback.format_exc()}")
            return False


class SMSDeliveryPlaceholder(DeliveryTarget):
    """SMS delivery placeholder with logging"""
    
    def __init__(self, name: str = "sms_placeholder", enabled: bool = True):
        self.name = name
        self.enabled = enabled
    
    async def deliver(self, content: Dict[str, Any], **kwargs) -> bool:
        """Log SMS would be sent to demonstrate capability"""
        if not self.enabled:
            _LOGGER.info(f"SMS delivery disabled - would send: {json.dumps(content)[:200]}...")
            return False
            
        phone_numbers = kwargs.get('recipients', []) or kwargs.get('phone_numbers', [])
        
        if phone_numbers:
            _LOGGER.info(
                f"SMS would be sent to {phone_numbers} - message: "
                f"'{content.get('content', 'Alert received')}'. "
                f"This is a placeholder - SMS provider not configured."
            )
        else:
            _LOGGER.warning(
                "SMS delivery requested but no recipients provided. "
                "Message would have contained: "
                f"'{content.get('content', 'Alert')[:100]}...'"
            )
        
        return True  # Count as "delivered" to prevent retries


class DeliveryRouter:
    """Main delivery routing system with priority-based delivery"""
    
    def __init__(self, targets: Optional[List[DeliveryTarget]] = None):
        self.targets = targets or []
        self.failed_targets = set()  # Track permanently failed targets
        self.logger = logging.getLogger(f"{__name__}.DeliveryRouter")
    
    def add_target(self, target: DeliveryTarget) -> None:
        """Add a delivery target to the router"""
        self.targets.append(target)
        if target.name in self.failed_targets:
            self.failed_targets.remove(target.name)
        self.logger.info(f"Added delivery target: {target.name}")
    
    def remove_target(self, target_name: str) -> bool:
        """Remove a delivery target by name"""
        original_count = len(self.targets)
        self.targets = [t for t in self.targets if t.name != target_name]
        if len(self.targets) < original_count:
            self.logger.info(f"Removed delivery target: {target_name}")
            return True
        return False
    
    async def deliver_to_all(self, content: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        """
        Attempt to deliver content to all configured targets.
        
        Returns a status dictionary with success/failure for each target.
        """
        if not self.targets:
            self.logger.warning("No delivery targets configured!")
            return {
                "success": False,
                "results": {},
                "errors": True,
                "message": "No delivery targets configured"
            }
        
        results = {}
        successful_deliveries = 0
        total_targets = len(self.targets)
        
        for target in self.targets:
            target_result = {
                'success': False,
                'error': None,
                'attempted': True
            }
            
            # Skip failed targets temporarily unless explicitly retried
            if target.name in self.failed_targets and not kwargs.get('force_retry', False):
                target_result['error'] = "Target previously failed and marked as down"
                target_result['attempted'] = False
                target_result['skipped'] = True
            else:
                try:
                    success = await target.deliver(content, **kwargs)
                    target_result['success'] = success
                    if success:
                        successful_deliveries += 1
                    else:
                        target_result['error'] = "Delivery failed"
                        
                except Exception as e:
                    target_result['error'] = str(e)
                    
                    # Mark as failed if too many consecutive errors
                    self.logger.error(f"Delivery target {target.name} failed: {str(e)}")
                    if kwargs.get('fail_on_error', False):
                        self.failed_targets.add(target.name)
        
            results[target.name] = target_result
        
        status = {
            'success': successful_deliveries > 0,
            'results': results,
            'successful_targets': successful_deliveries,
            'total_targets': total_targets,
            'errors': sum(1 for r in results.values() if r.get('error')) > 0,
            'all_failed': successful_deliveries == 0
        }
        
        # Log delivery summary
        successful_names = [name for name, res in results.items() if res['success']]
        failed_names = [name for name, res in results.items() if not res['success'] and not res.get('skipped')]
        skipped_names = [name for name, res in results.items() if res.get('skipped')]
        
        summary_parts = []
        if successful_names:
            summary_parts.append(f"SUCCESS: {', '.join(successful_names)}")
        if failed_names:
            summary_parts.append(f"FAILED: {', '.join(failed_names)}")
        if skipped_names:
            summary_parts.append(f"SKIPPED: {', '.join(skipped_names)}")
        
        self.logger.info(f"Delivery result: {' | '.join(summary_parts) if summary_parts else 'NO TARGETS'}")
        
        return status
    
    async def shutdown(self):
        """Clean up resources"""
        if hasattr(self, 'targets'):
            for target in self.targets:
                if hasattr(target, 'session') and target.session:
                    await target.session.close()


async def create_default_delivery_router() -> DeliveryRouter:
    """
    Create a delivery router with common configurations pulled from environment.
    """
    router = DeliveryRouter()
    
    # Add Discord if configured
    discord_webhook = os.getenv('ALERT_WEBHOOK_URL')
    if discord_webhook:
        router.add_target(DiscordDelivery(discord_webhook))
        _LOGGER.info("Discord delivery target added from ALERT_WEBHOOK_URL env var")
    else:
        _LOGGER.warning("No ALERT_WEBHOOK_URL found - Discord delivery not configured")
    
    # Add HTTP dashboard if configured
    dashboard_endpoint = os.getenv('DASHBOARD_ENDPOINT_URL')
    if dashboard_endpoint:
        router.add_target(HTTPDashboardDelivery(dashboard_endpoint))
        _LOGGER.info(f"HTTP dashboard delivery target added from DASHBOARD_ENDPOINT_URL: {dashboard_endpoint}")
    
    # Always add SMS placeholder (can be enabled with configuration)
    sms_enabled = os.getenv('SMS_NOTIFICATIONS_ENABLED', '').lower() in ['true', 'yes', '1']
    router.add_target(SMSDeliveryPlaceholder(enabled=sms_enabled))
    status_msg = "enabled" if sms_enabled else "disabled"
    _LOGGER.info(f"SMS delivery placeholder {status_msg}")
    
    return router


# Heartbeat delivery functionality
class HeartbeatDeliverer:
    """Handles delivering heartbeat information to multiple channels"""
    
    def __init__(self, delivery_router: DeliveryRouter):
        self.delivery_router = delivery_router
        self.last_heartbeat_sent = None
        self.logger = logging.getLogger(f"{__name__}.HeartbeatDeliverer")
    
    async def send_heartbeat(
        self,
        system_info: Dict[str, Any],
        channels: Optional[List[str]] = None,
        heartbeat_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Send heartbeat with system information.
        
        Args:
            system_info: Dictionary containing system status information
            channels: Limit to specific channels (e.g., ['discord', 'http_dashboard'])
            heartbeat_id: Unique identifier for this heartbeat
        
        Returns:
            Status dictionary from delivery attempt
        """
        heartbeat_content = {
            'content': None,
            'embeds': [{
                'title': '📡 Weather Engine Heartbeat',
                'description': f"System Status Report\n{system_info.get('status', 'Online')}",
                'color': 0x00ff00,  # Green
                'fields': [
                    # Convert system info to embed fields
                    {'name': key.replace('_', ' ').title(), 
                     'value': str(value), 
                     'inline': True} 
                     for key, value in system_info.items() 
                     if key != 'status'
                ],
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'footer': {
                    'text': f"Heartbeat ID: {heartbeat_id or 'auto'} | Sent at"
                }
            }]
        }
        
        if channels:
            # If specific channels were requested, temporarily filter
            original_targets = self.delivery_router.targets
            filtered_targets = [t for t in original_targets if t.name in channels]
            
            # Create temporary router for targeted delivery
            temp_router = DeliveryRouter(filtered_targets)
            result = await temp_router.deliver_to_all(heartbeat_content, type='heartbeat')
        else:
            # Deliver to all configured channels
            result = await self.delivery_router.deliver_to_all(heartbeat_content, type='heartbeat')
        
        if result['success']:
            self.logger.info(f"Heartbeat #{heartbeat_id or 'auto'} successfully sent to {result['successful_targets']}/{result['total_targets']} channel(s)")
        else:
            self.logger.warning(f"Heartbeat #{heartbeat_id or 'auto'} failed to reach any delivery channel")
        
        return result


# Example usage and testing
async def main():
    """Test the delivery router"""
    # Create delivery router with sample configuration 
    router = await create_default_delivery_router()
    
    if not router.targets:
        print("No delivery targets configured, creating test targets...")
        # Add mock targets if no real ones configured
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as f:
            test_file = f.name
            
        # Mock delivery for test purposes
        router.targets = [SMSDeliveryPlaceholder(enabled=True)]
    
    # Sample alert content
    sample_content = {
        "content": "**Weather Alert**: Temperature crossing detected",
        "embeds": [{
            "title": "🌡️ Temperature Alert",
            "description": "Temperature crossed threshold",
            "color": 0xFF0000,
            "fields": [
                {"name": "Station", "value": "KATL", "inline": True},
                {"name": "Market", "value": "HIGH", "inline": True},
                {"name": "Value", "value": "75°F", "inline": True},
            ]
        }]
    }
    
    # Test delivery
    print("Testing delivery to all configured targets...")
    result = await router.deliver_to_all(sample_content, username="Test Alert")
    print(f"Delivery result: {result}")
    
    # Test heartbeat
    heartbeat_info = {
        "status": "online",
        "signal_count": 42,
        "last_alert_time": "2023-01-01T12:00:00Z",
        "account_balance": "$1,234.56",
        "kill_switch": "active",
        "uptime_hours": 48.5
    }
    
    hb_deliverer = HeartbeatDeliverer(router)
    print("Testing heartbeat delivery...")
    hb_result = await hb_deliverer.send_heartbeat(heartbeat_info)
    print(f"Heartbeat result: {hb_result}")
    
    # Close router
    await router.shutdown()

if __name__ == "__main__":
    asyncio.run(main())