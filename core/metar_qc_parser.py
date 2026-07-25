#!/usr/bin/env python3
"""
A5 — METAR QC Flag Parser

Parses METAR quality control flags from the raw METAR text string and provides
a quality assessment that can be used to filter observations before spike
detection processing.

Key METAR QC flags parsed:
  - AO1/AO2: Automated station type
  - AUTO: Fully automated observation
  - COR: Corrected observation (retransmission)
  - $ (dollar sign): Maintenance indicator
  - VIRGA: Virga (precipitation evaporating aloft — can cause false temp drops)
  - TS/TSRA: Thunderstorm (rapid temp changes, irregular conditions)
  - FZRA/FZDZ: Freezing precipitation (sensor icing risk)
  - TS/RA/SN/GS/GR: Precipitation types
  - SPECI: Special observation (non-routine)

Usage:
    from core.metar_qc_parser import METARQualityParser, QualityAssessment

    parser = METARQualityParser()
    assessment = parser.parse("KATL 032052Z ... RMK AO2 VIRGA $")
    if assessment.passes_filter:
        # Include observation in spike detection
    else:
        # Observation has quality issues, skip or discount
"""

import re
from typing import Dict, Optional, Tuple
from enum import IntEnum


# ─── Quality Tiers ──────────────────────────────────────────────

class QualityTier(IntEnum):
    """Quality tiers for METAR observations."""
    GOOD = 4        # No flags, or only standard AO1/AO2
    ACCEPTABLE = 3  # Minor flags (AUTO, non-freezing precip)
    SUSPECT = 2     # Moderate flags ($ maintenance, VIRGA present)
    POOR = 1        # Significant flags (FZRA, sensor issues)
    REJECT = 0      # Unusable (no data, corrupt)


# ─── Flag Definitions ───────────────────────────────────────────

# Flag category constants
FLAG_AO1 = "AO1"           # Auto station, no precip discriminator
FLAG_AO2 = "AO2"           # Auto station, with precip discriminator
FLAG_AUTO = "AUTO"         # Fully automated observation
FLAG_COR = "COR"           # Corrected observation
FLAG_MAINTENANCE = "$"      # Maintenance indicator
FLAG_VIRGA = "VIRGA"        # Virga (precip evaporating aloft)
FLAG_TS = "TS"             # Thunderstorm
FLAG_TSRA = "TSRA"         # Thunderstorm with rain
FLAG_FZRA = "FZRA"         # Freezing rain
FLAG_FZDZ = "FZDZ"         # Freezing drizzle
FLAG_SPECI = "SPECI"       # Special observation
FLAG_RERA = "RERA"         # Recent rain
FLAG_RESN = "RESN"         # Recent snow
FLAG_DRSN = "DRSN"         # Blowing snow
FLAG_BLSN = "BLSN"         # Blowing snow (high)
FLAG_SS = "SS"             # Sandstorm
FLAG_DS = "DS"             # Duststorm
FLAG_PY = "PY"             # Spray
FLAG_BC = "BC"             # Patchy
FLAG_PR = "PR"             # Partial
FLAG_MI = "MI"             # Shallow
FLAG_VC = "VC"             # Vicinity
FLAG_GR = "GR"             # Hail
FLAG_GS = "GS"             # Small hail/snow pellets

# Thunderstorm and severe weather patterns
SEVERE_PATTERNS = re.compile(
    r'\b(TS|TSRA|TSSN|TSGR|TSGS|FZRA|FZDZ|GR|GS|SS|DS)\b'
)

# Precipitation patterns
PRECIP_PATTERNS = re.compile(
    r'\b(RA|SHRA|SN|SHSN|DZ|PL|SG|IC|UP)\b'
)


class QualityAssessment:
    """
    Result of a METAR quality assessment.

    Attributes:
        raw_flags: Set of all QC flags found in the observation
        tier: Overall quality tier
        passes_filter: True if observation passes QC filter for spike detection
        requires_discount: True if observation should be confidence-discounted
        rejection_reason: Reason if passes_filter is False
        maintenance_indicator: True if $ maintenance flag present
        has_thunderstorm: True if thunderstorm activity present
        has_virga: True if virga present
        is_auto: True if AUTO flag present (fully automated)
        is_corrected: True if COR flag present (corrected obs)
        has_freezing_precip: True if freezing precipitation present
    """

    def __init__(
        self,
        raw_flags: set,
        tier: QualityTier,
        passes_filter: bool = True,
        requires_discount: bool = False,
        rejection_reason: Optional[str] = None,
        maintenance_indicator: bool = False,
        has_thunderstorm: bool = False,
        has_virga: bool = False,
        is_auto: bool = False,
        is_corrected: bool = False,
        has_freezing_precip: bool = False,
        has_precipitation: bool = False,
        precip_types: Optional[list] = None,
    ):
        self.raw_flags = raw_flags
        self.tier = tier
        self.passes_filter = passes_filter
        self.requires_discount = requires_discount
        self.rejection_reason = rejection_reason
        self.maintenance_indicator = maintenance_indicator
        self.has_thunderstorm = has_thunderstorm
        self.has_virga = has_virga
        self.is_auto = is_auto
        self.is_corrected = is_corrected
        self.has_freezing_precip = has_freezing_precip
        self.has_precipitation = has_precipitation
        self.precip_types = precip_types or []

    def to_dict(self) -> Dict:
        return {
            "tier": int(self.tier),
            "tier_label": self.tier.name,
            "passes_filter": self.passes_filter,
            "requires_discount": self.requires_discount,
            "rejection_reason": self.rejection_reason,
            "maintenance_indicator": self.maintenance_indicator,
            "has_thunderstorm": self.has_thunderstorm,
            "has_virga": self.has_virga,
            "is_auto": self.is_auto,
            "is_corrected": self.is_corrected,
            "has_freezing_precip": self.has_freezing_precip,
            "has_precipitation": self.has_precipitation,
            "precip_types": self.precip_types,
            "raw_flags": sorted(self.raw_flags),
        }

    def __repr__(self) -> str:
        return (f"<QualityAssessment tier={self.tier.name} "
                f"pass={self.passes_filter} flags={sorted(self.raw_flags)}>")


class METARQualityParser:
    """
    Parses METAR QC flags from raw METAR text and assesses observation quality.

    The parser extracts flags from:
    1. The body of the METAR (COR, AUTO, SPECI)
    2. The RMK (Remarks) section (AO1, AO2, $, VIRGA, TS, etc.)
    """

    # Pattern for RMK section — everything after "RMK"
    RMK_PATTERN = re.compile(r'\bRMK\b(.+)$', re.IGNORECASE)

    # Maintenance indicator — dollar sign at end (often with space before)
    MAINT_PATTERN = re.compile(r'\$\s*$|\s\+\$')

    # VIRGA appears in remarks
    VIRGA_PATTERN = re.compile(r'\bVIRGA\b')

    # Thunderstorm in body or remarks
    TS_PATTERN = re.compile(r'\b(TS|TSRA|TSSN|TSGR)\b')

    # COR in body (before station code, or in remarks)
    COR_PATTERN = re.compile(r'\bCOR\b')

    # AUTO in body (after time)
    AUTO_PATTERN = re.compile(r'\bAUTO\b')

    # AO1/AO2 in remarks
    AO_PATTERN = re.compile(r'\b(AO[12])\b')

    # SPECI
    SPECI_PATTERN = re.compile(r'\bSPECI\b')

    # Freezing precipitation
    FREEZING_PATTERN = re.compile(r'\b(FZRA|FZDZ|FZFG)\b')

    def parse(self, raw_metar: Optional[str]) -> QualityAssessment:
        """
        Parse a raw METAR string and return a quality assessment.

        Args:
            raw_metar: The raw METAR text string

        Returns:
            QualityAssessment with parsed flags and quality tier
        """
        if not raw_metar or not isinstance(raw_metar, str):
            return QualityAssessment(
                raw_flags=set(),
                tier=QualityTier.REJECT,
                passes_filter=False,
                rejection_reason="empty_or_invalid",
            )

        raw_flags = set()

        # Extract RMK section
        rmk_match = self.RMK_PATTERN.search(raw_metar)
        rmk_text = rmk_match.group(1) if rmk_match else ""

        # Parse: AO1/AO2
        ao_match = self.AO_PATTERN.search(raw_metar)
        if ao_match:
            raw_flags.add(ao_match.group(1))

        # Parse: $ maintenance indicator
        has_maint = bool(self.MAINT_PATTERN.search(raw_metar))
        if has_maint:
            raw_flags.add("$")

        # Parse: VIRGA
        has_virga = bool(self.VIRGA_PATTERN.search(raw_metar))
        if has_virga:
            raw_flags.add("VIRGA")

        # Parse: Thunderstorm
        has_ts = bool(self.TS_PATTERN.search(raw_metar))
        if has_ts:
            raw_flags.add("TS")

        # Parse: COR
        is_corrected = bool(self.COR_PATTERN.search(raw_metar))
        if is_corrected:
            raw_flags.add("COR")

        # Parse: AUTO
        is_auto = bool(self.AUTO_PATTERN.search(raw_metar))
        if is_auto:
            raw_flags.add("AUTO")

        # Parse: SPECI
        is_speci = bool(self.SPECI_PATTERN.search(raw_metar))
        if is_speci:
            raw_flags.add("SPECI")

        # Parse: Freezing precipitation
        has_freezing = bool(self.FREEZING_PATTERN.search(raw_metar))
        if has_freezing:
            raw_flags.add("FZRA/FZDZ")

        # Parse: Severe weather patterns
        severe_types = []
        for match in SEVERE_PATTERNS.finditer(raw_metar):
            raw_flags.add(match.group(1))
            severe_types.append(match.group(1))

        # Parse: Precipitation types
        precip_types = []
        for match in PRECIP_PATTERNS.finditer(raw_metar):
            flag = match.group(1)
            if flag not in raw_flags:
                raw_flags.add(flag)
            precip_types.append(flag)

        has_precipitation = len(precip_types) > 0
        has_thunderstorm = has_ts or "TS" in raw_flags or "TSRA" in raw_flags

        # Determine quality tier and filter pass
        tier, passes_filter, requires_discount, reason = self._assess_quality(
            raw_flags=raw_flags,
            has_maint=has_maint,
            has_virga=has_virga,
            has_thunderstorm=has_thunderstorm,
            has_freezing=has_freezing,
            is_corrected=is_corrected,
        )

        return QualityAssessment(
            raw_flags=raw_flags,
            tier=tier,
            passes_filter=passes_filter,
            requires_discount=requires_discount,
            rejection_reason=reason,
            maintenance_indicator=has_maint,
            has_thunderstorm=has_thunderstorm,
            has_virga=has_virga,
            is_auto=is_auto,
            is_corrected=is_corrected,
            has_freezing_precip=has_freezing,
            has_precipitation=has_precipitation,
            precip_types=precip_types,
        )

    def _assess_quality(
        self,
        raw_flags: set,
        has_maint: bool = False,
        has_virga: bool = False,
        has_thunderstorm: bool = False,
        has_freezing: bool = False,
        is_corrected: bool = False,
    ) -> Tuple[QualityTier, bool, bool, Optional[str]]:
        """
        Determine quality tier and filter decision based on observed flags.

        Returns:
            (tier, passes_filter, requires_discount, rejection_reason)
        """
        reasons = []

        # REJECT: Corrupt or no-data flags (not a flag, but structural)
        # (handled at parse level with empty raw_metar)

        # POOR: Freezing precipitation — sensor icing risk
        if has_freezing:
            return (
                QualityTier.POOR,
                False,
                False,
                "freezing_precipitation_sensor_risk",
            )

        # SUSPECT: VIRGA — can cause false temperature drops
        if has_virga:
            return (
                QualityTier.SUSPECT,
                True,
                True,  # Requires discount — spike may be false
                None,
            )

        # SUSPECT: Thunderstorm — rapid, irregular temp changes
        if has_thunderstorm:
            return (
                QualityTier.SUSPECT,
                True,
                True,  # Requires discount — temp may be transient
                None,
            )

        # ACCEPTABLE: Maintenance indicator — too common to block, but note
        if has_maint:
            return (
                QualityTier.ACCEPTABLE,
                True,
                False,
                None,
            )

        # ACCEPTABLE: Corrected observation
        if is_corrected:
            return (
                QualityTier.ACCEPTABLE,
                True,
                False,
                None,
            )

        # GOOD: No flags or only standard AO1/AO2
        return (
            QualityTier.GOOD,
            True,
            False,
            None,
        )

    @staticmethod
    def parse_batch(raw_metars: list) -> list:
        """
        Parse a batch of METAR observations.

        Args:
            raw_metars: List of raw METAR strings

        Returns:
            List of QualityAssessment objects
        """
        parser = METARQualityParser()
        return [parser.parse(m) for m in raw_metars]

    @staticmethod
    def get_quality_weight(assessment: QualityAssessment) -> float:
        """
        Get a numeric weight [0.0-1.0] for use in signal confidence adjustment.

        GOOD = 1.0
        ACCEPTABLE = 0.9
        SUSPECT = 0.6
        POOR = 0.3
        REJECT = 0.0

        Args:
            assessment: QualityAssessment from parse()

        Returns:
            Weight multiplier for signal confidence
        """
        weights = {
            QualityTier.GOOD: 1.0,
            QualityTier.ACCEPTABLE: 0.9,
            QualityTier.SUSPECT: 0.6,
            QualityTier.POOR: 0.3,
            QualityTier.REJECT: 0.0,
        }
        return weights.get(assessment.tier, 0.0)

    @staticmethod
    def discount_confidence(assessment: QualityAssessment, confidence: float) -> float:
        """
        Apply quality-based discount to a spike detection confidence score.

        Args:
            assessment: QualityAssessment from parse()
            confidence: Original confidence score [0.0, 1.0]

        Returns:
            Discounted confidence score
        """
        weight = METARQualityParser.get_quality_weight(assessment)
        return confidence * weight


# ─── Self-Test ──────────────────────────────────────────────────

def _self_test():
    """Run basic validation of the METAR QC flag parser."""
    parser = METARQualityParser()

    # Test 1: Standard METAR with AO2 only (GOOD)
    r1 = parser.parse("KATL 032052Z 20003KT 10SM FEW032 27/21 A3008 RMK AO2 SLP176")
    assert r1.tier == QualityTier.GOOD, f"Expected GOOD, got {r1.tier}"
    assert r1.passes_filter
    assert not r1.requires_discount
    assert not r1.has_thunderstorm
    assert not r1.has_virga
    assert METARQualityParser.get_quality_weight(r1) == 1.0
    print(f"  Test 1 PASS: {r1}")

    # Test 2: METAR with $ maintenance indicator (ACCEPTABLE)
    r2 = parser.parse("KBOS 032054Z 27011KT 10SM FEW080 37/17 A2981 RMK AO2 SLP094 T03670167 $")
    assert r2.tier == QualityTier.ACCEPTABLE
    assert r2.passes_filter
    assert r2.maintenance_indicator
    assert METARQualityParser.get_quality_weight(r2) == 0.9
    print(f"  Test 2 PASS: {r2}")

    # Test 3: METAR with VIRGA (SUSPECT, requires discount)
    r3 = parser.parse("KATL 032052Z 20003KT 10SM FEW032 27/21 A3008 RMK AO2 VIRGA OHD-ALQDS")
    assert r3.tier == QualityTier.SUSPECT
    assert r3.passes_filter  # passes filter but with discount
    assert r3.requires_discount
    assert r3.has_virga
    discount = METARQualityParser.discount_confidence(r3, 0.70)
    assert abs(discount - 0.42) < 0.01, f"Expected 0.42, got {discount}"
    print(f"  Test 3 PASS: {r3}")

    # Test 4: METAR with thunderstorm (SUSPECT, requires discount)
    r4 = parser.parse("KMDW 032117Z 19007KT 7SM -TSRA SCT044 24/21 A3000 RMK AO2 FRQ LTGICCG TS SE-SW MOV E")
    assert r4.tier == QualityTier.SUSPECT
    assert r4.passes_filter
    assert r4.requires_discount
    assert r4.has_thunderstorm
    print(f"  Test 4 PASS: {r4}")

    # Test 5: Empty/NULL METAR (REJECT)
    r5 = parser.parse(None)
    assert r5.tier == QualityTier.REJECT
    assert not r5.passes_filter
    assert not r5.requires_discount
    discount = METARQualityParser.discount_confidence(r5, 0.70)
    assert discount == 0.0
    print(f"  Test 5 PASS: {r5}")

    # Test 6: AUTO METAR
    r6 = parser.parse("KDEN 032053Z VRB04KT 10SM AUTO FEW100 31/M03 A3008 RMK A02")
    assert r6.tier == QualityTier.GOOD  # AUTO alone is GOOD
    assert r6.is_auto
    assert r6.passes_filter
    print(f"  Test 6 PASS: {r6}")

    # Test 7: to_dict round-trip
    r7 = parser.parse("KATL 032052Z 20003KT 10SM FEW032 27/21 A3008 RMK AO2")
    d = r7.to_dict()
    assert d["tier"] == int(QualityTier.GOOD)
    assert d["tier_label"] == "GOOD"
    assert d["passes_filter"] is True
    print(f"  Test 7 PASS: dict={d['tier_label']}")

    # Test 8: FZRA (POOR, REJECT)
    r8 = parser.parse("KATL 010852Z 20003KT 10SM FZRA OVC032 27/21 A3008 RMK AO2")
    assert r8.tier == QualityTier.POOR
    assert not r8.passes_filter
    assert r8.has_freezing_precip
    r8b = parser.parse("KATL 010852Z 20003KT 10SM FZDZ OVC032 27/21 A3008 RMK AO2")
    assert r8b.tier == QualityTier.POOR
    assert not r8b.passes_filter
    print(f"  Test 8 PASS: FZRA={r8.rejection_reason}, FZDZ={r8b.rejection_reason}")

    # Test 9: parse_batch
    raw_batch = [
        "KATL 032052Z 20003KT 10SM FEW032 27/21 A3008 RMK AO2",
        "KBOS 032054Z 27011KT 10SM FEW080 37/17 A2981 RMK AO2 $",
        None,
    ]
    results = METARQualityParser.parse_batch(raw_batch)
    assert len(results) == 3
    assert results[0].tier == QualityTier.GOOD
    assert results[1].tier == QualityTier.ACCEPTABLE
    assert results[2].tier == QualityTier.REJECT
    print(f"  Test 9 PASS: batch of {len(results)} parsed")

    # Test 10: COR
    r10 = parser.parse("COR KATL 032052Z 20003KT 10SM FEW032 27/21 A3008 RMK AO2")
    assert r10.tier == QualityTier.ACCEPTABLE
    assert r10.is_corrected
    assert r10.passes_filter
    print(f"  Test 10 PASS: {r10}")

    print("\nAll self-tests PASS")
    return True


if __name__ == "__main__":
    _self_test()
