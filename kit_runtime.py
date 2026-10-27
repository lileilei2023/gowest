"""Source/native entry-point selection. Native releases use Nuitka onefile."""
from pathlib import Path
import shutil
import sys


def compiled():
    return '__compiled__' in globals()


def executable():
    # In Nuitka onefile, argv[0] is the original executable; __file__ and
    # sys.executable may point into the temporary extraction directory.
    candidate = Path(sys.argv[0]).absolute()
    if not candidate.is_file():
        found = shutil.which(sys.argv[0])
        if not found:
            raise RuntimeError('Cannot locate the original native executable')
        candidate = Path(found).absolute()
    return candidate.resolve()


def command(arguments, root=None):
    if compiled():
        return [str(executable())] + list(arguments)
    directory = Path(root) if root else Path(__file__).resolve().parent
    return [sys.executable, '-B', str(directory / 'launcher.py')] + list(arguments)
