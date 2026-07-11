from .acoustid import AcoustIDClient
from .cover_art_archive import CoverArtArchiveClient
from .discogs import DiscogsClient
from .genius import GeniusClient
from .itunes import ITunesClient
from .deezer import DeezerClient
from .lastfm import LastFMClient
from .local_cleanup import LocalCleanupClient
from .local_ai_audio import EssentiaDiscogsEffnetClient, EssentiaFeaturesClient, MusicNNMtgJamendoClient
from .musicbrainz import MusicBrainzClient
from .spotify import SpotifyClient
from .sonoteller import SonotellerClient
from .wikidata import WikidataClient
from .web_discovery import WebDiscoveryClient
from .rules_inference import RulesInferenceClient

__all__ = [
    "AcoustIDClient",
    "CoverArtArchiveClient",
    "DiscogsClient",
    "GeniusClient",
    "ITunesClient",
    "DeezerClient",
    "LastFMClient",
    "LocalCleanupClient",
    "EssentiaFeaturesClient",
    "EssentiaDiscogsEffnetClient",
    "MusicNNMtgJamendoClient",
    "MusicBrainzClient",
    "SpotifyClient",
    "SonotellerClient",
    "WikidataClient",
    "WebDiscoveryClient",
    "RulesInferenceClient",
    "CoverArtArchiveClient",
]
