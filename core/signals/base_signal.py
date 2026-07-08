from abc import ABC, abstractmethod
from typing import Optional, Tuple, List
import numpy as np  # Make sure numpy is available to all signals


class BaseSignal(ABC):
    """
    Base class for all weather signals.
    Requires implementation of a min_lookback property and evaluate method.
    """
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name of the signal."""
        return "Base Signal"
    
    @property
    @abstractmethod
    def min_lookback(self) -> int:
        """Minimum number of previous days needed to compute signal."""
        pass

    @abstractmethod
    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """
        Evaluate the signal at index idx using the days data.
        
        Args:
            idx: Current index in the days list
            days: List of daily data dictionaries with fields like:
                  'date', 'high', 'low', 'temp', 'pressure', etc.
                  
        Returns:
            (direction: str or None, confidence: float) 
            direction: 'up' or 'down' if signal fires, None if it doesn't
            confidence: float between 0 and 1 indicating signal strength
        """
        pass