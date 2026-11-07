from parser.sources.wikibooks import WikibooksSource
from parser.sources.wikibooks_en import WikibooksEnSource
from parser.sources.wikibooks_foreign import FOREIGN_SOURCES

SOURCES = {
    WikibooksSource.name: WikibooksSource,
    WikibooksEnSource.name: WikibooksEnSource,
    **FOREIGN_SOURCES,
}
