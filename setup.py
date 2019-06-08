from setuptools import (
    find_packages,
    setup,
)

from irc_bot import __version__

setup(
    name="irc_bot",
    version=__version__,
    description="IRC Bot",
    author="Iwan in 't Groen",
    author_email="iwanintgroen@gmail.com",
    packages=find_packages(),
)
