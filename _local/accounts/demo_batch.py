"""Fictional, deterministic account set. Never opens files or makes network requests."""
class Demo:
    namespace = "fictional-v1"
    label = "Fictional demo — temporary database"

    def fetch(self):
        names = ["Cedar Credit", "Birch Bank", "Maple Trust"]
        connections = [dict(conn_id=str(i), name=name) for i, name in enumerate(names)]
        accounts = [{"conn_id": str(i // 4), "id": str(i % 4), "name": ["Card", "Savings", "Reserve", "Cash"][i % 4],
                     "balance-date": 1717243200, "currency": "JPY" if i >= 8 else "USD",
                     "balance": "-20.01" if i == 0 else "501" if i >= 8 else "10.10"}
                    for i in range(12)]
        return dict(connections=connections, accounts=accounts, errlist=[])


def seed(db):
    db.execute("""INSERT INTO accounts (institution,nickname,kind,side,currency,balance,asof,owner,update_method)
                  VALUES ('Cedar Credit','Household card','Credit card','debt','USD','30.00','2024-05-01',
                          'Joint','Monthly statement')""")
