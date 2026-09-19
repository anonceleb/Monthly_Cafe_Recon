import os

os.environ["RECON_DSN"] = "host=localhost dbname=kredo_recon_test user=ashwin"   # never the real database

import pytest

from recon import config, db


@pytest.fixture()
def conn():
    c = db.connect()
    db.reset(c)
    yield c
    c.close()


@pytest.fixture(scope="session")
def cfg():
    return config.load()
