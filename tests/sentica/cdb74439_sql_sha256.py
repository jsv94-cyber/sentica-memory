"""sha256 of the SQL frozen at main cdb74439.

The vendor copy and the migration files are both hashed. Changing either
string, or changing both to the same new string, fails the freeze test.
"""

FROZEN_SQL_SHA256: dict[str, str] = {
    "workspace_policy:memory:messages": "c8d452a4747cd8cc3987229032726a398d06c75dabd80b5c175f37f5ed8b2728",
    "workspace_policy:public:queue": "de4fd188b3f8475496969234b228f053165acd8963c58951d1226710aa8f3e15",
    "pending_queue:memory": "d0cff841124d8d45f7f67fd7a47c286b0d247cdbc74e357f1a2a7b08269fc24c",
    "pending_counts:memory": "1e83499e60038ff1eb65998d468fa44425b88c512ccf346b478291d8a102122c",
    "ddl_select:memory": "dc2ee7f794c2f8c7d9f07b845df10713df55a858a47c4066e97dc97b03f9312a",
    "pending_queue:public": "a243866d9f4dc8831e1c8b5facdae0401098b763489b63d7d0576e412e8e5757",
    "pending_counts:public": "a5610730e6d2f7dd0fc5d90940ef6e1f3a2b13c6fd95161b096c26763a27e26f",
    "ddl_select:public": "e12dc3d171d7bcff45c17e69f86b458bd74dfd5b009f702a95223c14de2095f1",
}
