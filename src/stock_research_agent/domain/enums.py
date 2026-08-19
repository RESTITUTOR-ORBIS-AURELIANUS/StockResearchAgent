"""领域枚举：把系统允许出现的字符串限制在明确集合中。"""

from enum import StrEnum


class TargetType(StrEnum):
    MARKET = "MARKET"
    SECTOR = "SECTOR"
    STOCK = "STOCK"


class EvidenceDomain(StrEnum):
    TECHNICAL = "TECHNICAL"
    FUNDAMENTAL = "FUNDAMENTAL"
    EVENT = "EVENT"
    SENTIMENT_FLOW = "SENTIMENT_FLOW"
    MACRO = "MACRO"


class VerificationStatus(StrEnum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    CONFLICTING = "CONFLICTING"
    REVISED = "REVISED"
    RETRACTED = "RETRACTED"


class ThesisDirection(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    MIXED = "MIXED"


class ThesisOriginType(StrEnum):
    LEAD_STRATEGIST = "LEAD_STRATEGIST"
    VALIDATOR_DISCOVERY = "VALIDATOR_DISCOVERY"


class ThesisValidationStatus(StrEnum):
    UNVERIFIED = "UNVERIFIED"
    UNDER_REVIEW = "UNDER_REVIEW"
    SUPPORTED = "SUPPORTED"
    REFUTED = "REFUTED"
    MIXED = "MIXED"
    INCONCLUSIVE = "INCONCLUSIVE"


class ResearchPriority(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ResearchRequestStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    NO_NEW_EVIDENCE = "NO_NEW_EVIDENCE"
    FAILED = "FAILED"
    CANCELLED_BY_BUDGET = "CANCELLED_BY_BUDGET"


class RecommendationProfile(StrEnum):
    AGGRESSIVE = "AGGRESSIVE"
    CONSERVATIVE = "CONSERVATIVE"
    CONSENSUS = "CONSENSUS"


class RecommendationAction(StrEnum):
    BUY = "BUY"
    OVERWEIGHT = "OVERWEIGHT"
    HOLD = "HOLD"
    UNDERWEIGHT = "UNDERWEIGHT"
    SELL = "SELL"
    AVOID = "AVOID"


class DecisionDimension(StrEnum):
    TARGET = "TARGET"
    ACTION = "ACTION"
    POSITION_SIZE = "POSITION_SIZE"
    ENTRY_STRATEGY = "ENTRY_STRATEGY"
    EXIT_STRATEGY = "EXIT_STRATEGY"
    VALUATION = "VALUATION"
    HORIZON = "HORIZON"
    RISK_CONTROL = "RISK_CONTROL"


class ProposalStatus(StrEnum):
    PROPOSED = "PROPOSED"
    NEGOTIATING = "NEGOTIATING"
    AGREED = "AGREED"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"
    ARBITRATED = "ARBITRATED"


class DebateStatus(StrEnum):
    AGREED = "AGREED"
    PARTIALLY_ARBITRATED = "PARTIALLY_ARBITRATED"
    ARBITRATED = "ARBITRATED"
    DISAGREED = "DISAGREED"


class PortfolioManager(StrEnum):
    AGGRESSIVE = "AggressivePortfolioManager"
    CONSERVATIVE = "ConservativePortfolioManager"
