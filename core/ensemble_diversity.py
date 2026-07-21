"""
Ensemble Diversity Score Module

This module calculates diversity among signal votes and applies penalties
when signals are too aligned (indicating redundancy rather than true consensus).

When all signals agree on the same direction, it's usually a strong signal. 
But when they agree too often, it means the ensemble isn't diverse — the signals 
are redundant. This module penalizes low diversity.
"""

def compute_diversity_score(signal_votes):
    """
    Calculate diversity score based on how dispersed signal votes are across directions.
    
    Args:
        signal_votes: List of (direction, confidence) tuples where direction is +1, -1, or 0
        
    Returns:
        float: Diversity score between 0 and 1, where:
            - 0 means all votes agreed (no diversity)
            - 1 means perfectly evenly distributed votes (maximum diversity)
    """
    if not signal_votes:
        return 1.0  # No votes means maximum diversity (edge case)
    
    # Count how many votes for each direction
    direction_counts = {}
    for direction, _ in signal_votes:
        direction_counts[direction] = direction_counts.get(direction, 0) + 1
    
    # Find the maximum vote share (percentage of votes in the majority direction)
    total_votes = len(signal_votes)
    max_votes = max(direction_counts.values()) if direction_counts else 0
    max_vote_share = max_votes / total_votes if total_votes > 0 else 0
    
    # Calculate diversity score: 1 - |2 * max_vote_share - 1|
    # - If all votes agree (max_vote_share = 1.0), diversity = 1 - |2*1 - 1| = 0
    # - If votes split evenly (max_vote_share = 0.5), diversity = 1 - |2*0.5 - 1| = 1
    diversity_score = 1 - abs(2 * max_vote_share - 1)
    
    return diversity_score


def apply_diversity_penalty(confidence, diversity_score):
    """
    Apply diversity penalty to confidence score.
    
    Args:
        confidence: Original confidence score
        diversity_score: Diversity score between 0 and 1
        
    Returns:
        float: Confidence score after applying diversity penalty
    """
    # Import from paper_trading_engine where config constants are defined
    try:
        from .paper_trading_engine import DIVERSITY_BASE_WEIGHT, DIVERSITY_MAX_WEIGHT
    except ImportError:
        # Fall back to default values if paper_trading_engine module cannot be imported
        DIVERSITY_BASE_WEIGHT = 0.75
        DIVERSITY_MAX_WEIGHT = 1.0

        # Allow override from environment variables
        import os
        DIVERSITY_BASE_WEIGHT = float(os.getenv("DIVERSITY_BASE_WEIGHT", DIVERSITY_BASE_WEIGHT))
        DIVERSITY_MAX_WEIGHT = float(os.getenv("DIVERSITY_MAX_WEIGHT", DIVERSITY_MAX_WEIGHT))
    
    # Calculate the penalty factor based on diversity
    # When diversity = 0 (all agree): penalty_factor = DIVERSITY_BASE_WEIGHT
    # When diversity = 1 (perfect split): penalty_factor = DIVERSITY_MAX_WEIGHT
    
    # Modulation factor: interpolate between BASE_WEIGHT and MAX_WEIGHT based on diversity
    penalty_factor = DIVERSITY_BASE_WEIGHT + (DIVERSITY_MAX_WEIGHT - DIVERSITY_BASE_WEIGHT) * diversity_score
    
    # Apply the penalty factor to the original confidence
    adjusted_confidence = confidence * penalty_factor
    
    return adjusted_confidence


if __name__ == "__main__":
    # Test the functions
    print("Testing ensemble diversity functions...")
    
    # Test case 1: Perfect disagreement (balanced)
    votes_balanced = [(1, 0.8), (-1, 0.7), (1, 0.9), (-1, 0.6)]
    div_balanced = compute_diversity_score(votes_balanced)
    print(f"Balanced votes {len(votes_balanced)}: diversity = {div_balanced:.3f}")
    
    # Test case 2: Full agreement
    votes_all_agree = [(1, 0.8), (1, 0.7), (1, 0.9), (1, 0.6)]
    div_all_agree = compute_diversity_score(votes_all_agree)
    print(f"All-agree votes {len(votes_all_agree)}: diversity = {div_all_agree:.3f}")
    
    # Test case 3: Partial agreement (5 out of 8)
    votes_partial = [(1, 0.8), (1, 0.7), (1, 0.9), (1, 0.6), (1, 0.5), (-1, 0.4), (-1, 0.3), (0, 0.2)]
    div_partial = compute_diversity_score(votes_partial)
    print(f"Partial votes {len(votes_partial)} - 5 for, 2 against, 1 neutral: diversity = {div_partial:.3f}")
    
    # Test the penalty function
    original_confidence = 0.9
    balanced_penalty = apply_diversity_penalty(original_confidence, div_balanced)
    all_agree_penalty = apply_diversity_penalty(original_confidence, div_all_agree)
    partial_penalty = apply_diversity_penalty(original_confidence, div_partial)
    
    print(f"\nOriginal confidence: {original_confidence}")
    print(f"Penalty applied with balanced votes: {balanced_penalty:.3f}")
    print(f"Penalty applied with full agreement ({div_all_agree:.3f}): {all_agree_penalty:.3f}")
    print(f"Penalty applied with partial agreement ({div_partial:.3f}): {partial_penalty:.3f}")