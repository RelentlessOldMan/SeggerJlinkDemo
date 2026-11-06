import pathlib

import pytest

CORPUS = pathlib.Path(__file__).parent / "corpus"

# Words in the synthetic corpus that stand in for private names. None may appear in an anonymized report.
PRIVATE_TOKENS = ["quux", "zeta", "zorblax", "frobnicate", "widget", "qxload", 
                  "grüße", "alpha", "beta", "gamma", "settings", "reset_handler", "mailbox", "cafef00d",
                  "top/", "lib/", "odd/", "broken/", "main_flash", "run_all", "dyn_dispatch", "common_init"]


@pytest.fixture(scope="session")
def corpus():
    from cmmrun.corpus import load_corpus
    return load_corpus(CORPUS)


@pytest.fixture()
def anon(corpus):
    from cmmrun.analysis.anonymize import Anonymizer
    return Anonymizer(corpus.vocab, b"test-salt-0123456789", enabled=True)
