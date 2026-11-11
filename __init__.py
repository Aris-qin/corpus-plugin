if not __package__:
    from pathlib import Path
    __package__ = __name__
    __path__ = [str(Path(__file__).resolve().parent)]
    if __spec__ is not None:
        __spec__.submodule_search_locations = __path__

from .corpus_core.plugin import register

__all__ = ['register']
