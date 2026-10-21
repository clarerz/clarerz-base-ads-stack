"""Object API for account management and ad publishing/reads."""

from publishing import AdPublisher
from validation import AccountValidator, coerce_id


class AdsService:
    def __init__(self, repository, publisher=None, account_validator=None):
        self.repository = repository
        self.publisher = publisher if publisher is not None else AdPublisher(repository)
        self.account_validator = (account_validator if account_validator is not None
                                  else AccountValidator())

    def create_account(self, account_id, name):
        account = self.account_validator.validate(account_id, name)
        self.repository.save_account(account)
        return account

    def get_account(self, account_id):
        account_id = coerce_id(account_id)
        return None if account_id is None else self.repository.get_account(account_id)

    def publish_ad(self, data):
        return self.publisher.publish(data)

    def get_ad(self, ad_id):
        ad_id = coerce_id(ad_id)
        return None if ad_id is None else self.repository.get_ad(ad_id)
