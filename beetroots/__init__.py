from .api import Matcher, MatcherConfig
from .schema import VERSION as __version__
from .schema import ArtistCredit, MatchIssue, MatchResult, TrackInput

__all__ = [
    "ArtistCredit",
    "MatchIssue",
    "MatchResult",
    "Matcher",
    "MatcherConfig",
    "TrackInput",
    "__version__",
]
