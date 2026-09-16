from setuptools import setup, find_packages

setup(
    name="herbaudit",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "pandas",
        "requests",
        "openpyxl",
        "python-dotenv",
        "ftfy",
        "unidecode",
        "python-Levenshtein",
        "urllib3",
        "openai",
        "tqdm",
        "tomli; python_version < '3.11'",
    ],
    entry_points={
        "console_scripts": [
            "herbaudit=herbaudit.cli:main",
        ],
    },
    author="Dilara Agacik",
    description="An AI-powered herbarium specimen audit tool",
    license="GPL-3.0-or-later",
    classifiers=[
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
    ],
    python_requires=">=3.8",
)