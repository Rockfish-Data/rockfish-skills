"""Generate synthetic data from schema specifications.

Demonstrates the GenerateFromDataSchema action with four examples:
  1. Simple device schema using a JSON dict.
  2. Users and sessions with a state machine and timeseries.
  3. Accounts and trades with a composite foreign key.
  4. Customers with NamedEntityProvider for realistic PII-like fields,
     plus a multilingual variant.

GenerateFromDataSchema supports:
  - Independent columns (IDs, categorical, numerical distributions)
  - Derived columns (computed via functions like MAP_VALUES)
  - Stateful columns (state machines, timeseries)
  - Entity relationships (foreign keys, including composite keys)
  - Temporal data with timestamps

Run:
    python examples/entity-gen.py               # run all four examples
    python examples/entity-gen.py -e 2          # run only example 2
    python examples/entity-gen.py -e 1 -e 3     # run examples 1 and 3
"""
import argparse
import asyncio

import rockfish as rf
import rockfish.actions as ra
from rockfish.actions.ent import (
    CategoricalParams,
    Column,
    ColumnCategoryType,
    ColumnType,
    DataSchema,
    Derivation,
    DerivationFunctionType,
    Domain,
    DomainType,
    Entity,
    EntityRelationship,
    EntityRelationshipType,
    GlobalTimestamp,
    IDParams,
    NamedEntityProvider,
    NamedEntityProviderParams,
    SampleFromColumnParams,
    StateMachineParams,
    Timestamp,
    TimeseriesParams,
    Transition,
    UniformDistParams,
)


async def example_1_device_schema(conn: rf.Connection) -> None:
    """Single entity with independent and derived columns.

    Creates a `devices` table with:
      device_id        - unique ID (template string)
      device_type      - categorical values
      status           - categorical values
      severity_level   - derived from status via MAP_VALUES
    """
    devices_schema_json = {
        "entities": [
            {
                "name": "devices",
                "cardinality": 20,
                "columns": [
                    {
                        "name": "device_id",
                        "data_type": "string",
                        "column_type": "independent",
                        "column_category_type": "metadata",
                        "domain": {
                            "type": "id",
                            "params": {"template_str": "DEVICE_{id}"},
                        },
                    },
                    {
                        "name": "device_type",
                        "data_type": "string",
                        "column_type": "independent",
                        "column_category_type": "metadata",
                        "domain": {
                            "type": "categorical",
                            "params": {
                                "values": ["router", "switch", "firewall"],
                                "weights": [0.4, 0.4, 0.2],
                                "with_replacement": True,
                                "seed": 10,
                            },
                        },
                    },
                    {
                        "name": "status",
                        "data_type": "string",
                        "column_type": "independent",
                        "column_category_type": "metadata",
                        "domain": {
                            "type": "categorical",
                            "params": {
                                "values": ["active", "idle", "maintenance", "down"],
                                "weights": [0.7, 0.15, 0.1, 0.05],
                                "with_replacement": True,
                                "seed": 30,
                            },
                        },
                    },
                    {
                        "name": "severity_level",
                        "data_type": "string",
                        "column_type": "derived",
                        "column_category_type": "metadata",
                        "derivation": {
                            "function_type": "map_values",
                            "dependent_columns": ["status"],
                            "params": {
                                "mapping": [
                                    {"from": "active", "to": "low"},
                                    {"from": "idle", "to": "low"},
                                    {"from": "maintenance", "to": "medium"},
                                    {"from": "down", "to": "critical"},
                                ],
                                "default": "unknown",
                            },
                        },
                    },
                ],
            }
        ],
        "entity_relationships": [],
    }

    generate = ra.GenerateFromDataSchema({
        "schema": devices_schema_json,
        "upload_datasets": True,
    })

    builder = rf.WorkflowBuilder()
    builder.add(generate)
    workflow = await builder.start(conn)
    print(f"Workflow ID: {workflow.id()}")
    await workflow.wait(raise_on_failure=True)

    async for log in workflow.logs(level=rf.events.LogLevel.DEBUG):
        print(log)

    remote_dataset = await workflow.datasets().nth(0)
    dataset = await remote_dataset.to_local(conn)
    df = dataset.to_pandas()
    print(df)

    print(f"Dataset name: {dataset.name()}")
    print(f"Number of rows: {dataset.table.num_rows}")
    print(f"Columns: {dataset.table.column_names}")

    print(df.describe())

    status_to_severity = {
        "active": "low",
        "idle": "low",
        "maintenance": "medium",
        "down": "critical",
    }
    print("Status -> Severity Level mapping validation:")
    for status, expected_severity in status_to_severity.items():
        actual = df[df["status"] == status]["severity_level"].unique()
        print(f"  {status} -> {actual} (expected: {expected_severity})")


async def example_2_user_sessions_state_machine(conn: rf.Connection) -> None:
    """Multiple entities with relationships, state machines, and timeseries.

    Creates:
      users     - user_id, username (sampled without replacement)
      sessions  - session_id, user_id (FK), page (state machine),
                  response_time_ms (timeseries)
    """
    schema = DataSchema(
        entities=[
            Entity(
                name="users",
                cardinality=50,
                columns=[
                    Column(
                        name="user_id",
                        data_type="string",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.ID,
                            params=IDParams(template_str="USER_{id}"),
                        ),
                    ),
                    Column(
                        name="username",
                        data_type="string",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.CATEGORICAL,
                            params=CategoricalParams(
                                values=[
                                    "alice", "bob", "charlie", "diana", "eve",
                                    "frank", "grace", "henry", "iris", "jack",
                                    "kate", "leo", "mary", "noah", "olivia",
                                    "paul", "quinn", "rachel", "steve", "tina",
                                    "uma", "victor", "wendy", "xander", "yara",
                                    "zoe", "adam", "bella", "carl", "dora",
                                    "ethan", "fiona", "george", "hanna", "ivan",
                                    "julia", "kyle", "laura", "mike", "nina",
                                    "oscar", "petra", "quentin", "rosa", "sam",
                                    "tara", "ursula", "vince", "wanda", "xavier",
                                ],
                                with_replacement=False,
                            ),
                        ),
                    ),
                ],
            ),
            Entity(
                name="sessions",
                cardinality=200,
                timestamp=Timestamp(column_name="timestamp"),
                columns=[
                    Column(
                        name="session_id",
                        data_type="string",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.ID,
                            params=IDParams(template_str="SESSION_{id}"),
                        ),
                    ),
                    Column(
                        name="user_id",
                        data_type="string",
                        column_type=ColumnType.DERIVED,
                        column_category_type=ColumnCategoryType.METADATA,
                        derivation=Derivation(
                            function_type=DerivationFunctionType.SAMPLE_FROM_COLUMN,
                            dependent_columns=["users.user_id"],
                            params=SampleFromColumnParams(with_replacement=True, seed=42),
                        ),
                    ),
                    Column(
                        name="page",
                        data_type="string",
                        column_type=ColumnType.STATEFUL,
                        column_category_type=ColumnCategoryType.MEASUREMENT,
                        domain=Domain(
                            type=DomainType.STATE_MACHINE,
                            params=StateMachineParams(
                                column_name="page",
                                trigger_column_name="action",
                                initial_state="homepage",
                                states=["homepage", "search", "product", "cart", "checkout", "exit"],
                                terminal_states=["exit"],
                                transitions=[
                                    Transition(trigger="browse",       source="homepage", dest="search",   probability=0.6),
                                    Transition(trigger="view_product", source="homepage", dest="product",  probability=0.3),
                                    Transition(trigger="leave",        source="homepage", dest="exit",     probability=0.1),
                                    Transition(trigger="view_product", source="search",   dest="product",  probability=0.7),
                                    Transition(trigger="leave",        source="search",   dest="exit",     probability=0.3),
                                    Transition(trigger="add_to_cart",  source="product",  dest="cart",     probability=0.5),
                                    Transition(trigger="leave",        source="product",  dest="exit",     probability=0.5),
                                    Transition(trigger="checkout",     source="cart",     dest="checkout", probability=0.7),
                                    Transition(trigger="leave",        source="cart",     dest="exit",     probability=0.3),
                                    Transition(trigger="complete",     source="checkout", dest="exit",     probability=1.0),
                                ],
                            ),
                        ),
                    ),
                    Column(
                        name="response_time_ms",
                        data_type="float64",
                        column_type=ColumnType.STATEFUL,
                        column_category_type=ColumnCategoryType.MEASUREMENT,
                        domain=Domain(
                            type=DomainType.TIMESERIES,
                            params=TimeseriesParams(
                                base_value=150.0,
                                min_value=50.0,
                                max_value=300.0,
                                seasonality_type="symmetric",
                                seasonality_strength=0.3,
                                noise_level=0.2,
                                seed=123,
                            ),
                        ),
                    ),
                ],
            ),
        ],
        entity_relationships=[
            EntityRelationship(
                parent_entity="users",
                child_entity="sessions",
                relationship_type=EntityRelationshipType.ONE_TO_MANY,
                join_columns={"user_id": "user_id"},
            )
        ],
        global_timestamp=GlobalTimestamp(
            t_start="2025-01-01T00:00:00Z",
            t_end="2025-01-01T01:00:00Z",
            time_interval="1min",
        ),
    )

    generate = ra.GenerateFromDataSchema(
        ra.GenerateFromDataSchema.Config(schema=schema, upload_datasets=True)
    )

    builder = rf.WorkflowBuilder()
    builder.add(generate)
    workflow = await builder.start(conn)
    print(f"Workflow ID: {workflow.id()}")
    await workflow.wait(raise_on_failure=True)

    async for log in workflow.logs(level=rf.events.LogLevel.DEBUG):
        print(log)

    datasets = await workflow.datasets().collect()
    print(f"Generated {len(datasets)} datasets")

    users_dataset = None
    sessions_dataset = None
    for remote_ds in datasets:
        ds = await remote_ds.to_local(conn)
        print(f"Found dataset: {ds.name()!r}")
        if ds.name() == "users":
            users_dataset = ds
        elif ds.name() == "sessions":
            sessions_dataset = ds

    users_df = users_dataset.to_pandas()
    print(f"Users dataset: {users_dataset.table.num_rows} rows")
    print(users_df.head())

    sessions_df = sessions_dataset.to_pandas()
    print(f"Sessions dataset: {sessions_dataset.table.num_rows} rows")
    print(sessions_df.head())

    session_user_ids = set(sessions_df["user_id"])
    valid_user_ids = set(users_df["user_id"])
    print(f"All session user_ids are valid: {session_user_ids.issubset(valid_user_ids)}")

    print("Page state distribution:")
    print(sessions_df["page"].value_counts())

    print("Action trigger distribution:")
    print(sessions_df["action"].value_counts())

    print("Response time statistics:")
    print(sessions_df["response_time_ms"].describe())

    first_session = sessions_df["session_id"].iloc[0]
    session_rows = sessions_df[sessions_df["session_id"] == first_session].sort_values("timestamp")
    print(f"Timestamps for session {first_session}:")
    print(session_rows[["timestamp", "page", "action", "response_time_ms"]])


async def example_3_trades_composite_fk(conn: rf.Connection) -> None:
    """Composite foreign keys: multiple columns form the relationship.

    Creates:
      accounts - (broker_id, account_number) as composite key
      trades   - references accounts via (trade_broker_id, trade_account_number)
    """
    schema = DataSchema(
        entities=[
            Entity(
                name="accounts",
                cardinality=10,
                columns=[
                    Column(
                        name="broker_id",
                        data_type="string",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.CATEGORICAL,
                            params=CategoricalParams(
                                values=["BROKER_A", "BROKER_B", "BROKER_C"],
                                with_replacement=True,
                                seed=100,
                            ),
                        ),
                    ),
                    Column(
                        name="account_number",
                        data_type="string",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.ID,
                            params=IDParams(template_str="ACC{id}"),
                        ),
                    ),
                    Column(
                        name="account_type",
                        data_type="string",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.CATEGORICAL,
                            params=CategoricalParams(
                                values=["individual", "joint", "corporate"],
                                weights=[0.6, 0.3, 0.1],
                                with_replacement=True,
                                seed=101,
                            ),
                        ),
                    ),
                ],
            ),
            Entity(
                name="trades",
                cardinality=30,
                columns=[
                    Column(
                        name="trade_id",
                        data_type="string",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.ID,
                            params=IDParams(template_str="TRD_{id}"),
                        ),
                    ),
                    Column(
                        name="trade_broker_id",
                        data_type="string",
                        column_type=ColumnType.FOREIGN_KEY,
                        column_category_type=ColumnCategoryType.METADATA,
                    ),
                    Column(
                        name="trade_account_number",
                        data_type="string",
                        column_type=ColumnType.FOREIGN_KEY,
                        column_category_type=ColumnCategoryType.METADATA,
                    ),
                    Column(
                        name="symbol",
                        data_type="string",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.CATEGORICAL,
                            params=CategoricalParams(
                                values=["AAPL", "GOOGL", "MSFT", "TSLA", "AMZN"],
                                with_replacement=True,
                                seed=200,
                            ),
                        ),
                    ),
                    Column(
                        name="quantity",
                        data_type="int64",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.CATEGORICAL,
                            params=CategoricalParams(
                                values=[10, 50, 100, 500],
                                weights=[0.4, 0.3, 0.2, 0.1],
                                with_replacement=True,
                                seed=201,
                            ),
                        ),
                    ),
                    Column(
                        name="price",
                        data_type="float64",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.UNIFORM_DIST,
                            params=UniformDistParams(lower=100.0, upper=500.0, seed=202),
                        ),
                    ),
                ],
            ),
        ],
        entity_relationships=[
            EntityRelationship(
                parent_entity="accounts",
                child_entity="trades",
                relationship_type=EntityRelationshipType.ONE_TO_MANY,
                join_columns={
                    "broker_id": "trade_broker_id",
                    "account_number": "trade_account_number",
                },
            )
        ],
    )

    generate = ra.GenerateFromDataSchema(
        ra.GenerateFromDataSchema.Config(schema=schema, upload_datasets=True)
    )

    builder = rf.WorkflowBuilder()
    builder.add(generate)
    workflow = await builder.start(conn)
    print(f"Workflow ID: {workflow.id()}")
    await workflow.wait(raise_on_failure=True)

    async for log in workflow.logs(level=rf.events.LogLevel.DEBUG):
        print(log)

    datasets = await workflow.datasets().collect()
    print(f"Generated {len(datasets)} datasets")

    accounts_dataset = None
    trades_dataset = None
    for remote_ds in datasets:
        ds = await remote_ds.to_local(conn)
        if ds.name() == "accounts":
            accounts_dataset = ds
        elif ds.name() == "trades":
            trades_dataset = ds

    accounts_df = accounts_dataset.to_pandas()
    print(f"Accounts dataset: {accounts_dataset.table.num_rows} rows")
    print(accounts_df.head())

    trades_df = trades_dataset.to_pandas()
    print(f"Trades dataset: {trades_dataset.table.num_rows} rows")
    print(trades_df.head())

    valid_pairs = set(zip(accounts_df["broker_id"], accounts_df["account_number"]))
    print(f"Valid account pairs: {len(valid_pairs)}")

    trade_pairs = set(zip(trades_df["trade_broker_id"], trades_df["trade_account_number"]))
    print(f"Trade pairs: {len(trade_pairs)}")

    invalid_pairs = trade_pairs - valid_pairs
    print(f"All trade pairs are valid: {len(invalid_pairs) == 0}")
    if invalid_pairs:
        print(f"Invalid pairs found: {invalid_pairs}")

    print("Trading symbol distribution:")
    print(trades_df["symbol"].value_counts())


async def example_4_named_entity_provider(conn: rf.Connection) -> None:
    """Realistic synthetic values via NamedEntityProvider.

    Creates a customers table using Mimesis (primary) / Faker (fallback)
    providers for:
      customer_id   - UUID via the cryptographic provider
      first_name    - localized first name
      last_name     - localized last name
      email         - realistic email addresses (unique within the pool)
      city          - city names in a chosen locale
      ssn           - US SSN via the Faker fallback

    Also runs a multilingual variant using the German locale.
    """
    customers_schema = DataSchema(
        entities=[
            Entity(
                name="customers",
                cardinality=50,
                columns=[
                    Column(
                        name="customer_id",
                        data_type="string",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.NAMED_ENTITY_PROVIDER,
                            params=NamedEntityProviderParams(
                                provider=NamedEntityProvider.CRYPTOGRAPHIC_UUID,
                                unique_values=1000,
                                with_replacement=False,
                                seed=1,
                            ),
                        ),
                    ),
                    Column(
                        name="first_name",
                        data_type="string",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.NAMED_ENTITY_PROVIDER,
                            params=NamedEntityProviderParams(
                                provider=NamedEntityProvider.PERSON_FIRST_NAME,
                                locale="en_us",
                                seed=2,
                            ),
                        ),
                    ),
                    Column(
                        name="last_name",
                        data_type="string",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.NAMED_ENTITY_PROVIDER,
                            params=NamedEntityProviderParams(
                                provider=NamedEntityProvider.PERSON_LAST_NAME,
                                locale="en_us",
                                seed=3,
                            ),
                        ),
                    ),
                    Column(
                        name="email",
                        data_type="string",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.NAMED_ENTITY_PROVIDER,
                            params=NamedEntityProviderParams(
                                provider=NamedEntityProvider.PERSON_EMAIL,
                                unique_values=200,
                                with_replacement=False,
                                seed=4,
                            ),
                        ),
                    ),
                    Column(
                        name="city",
                        data_type="string",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.NAMED_ENTITY_PROVIDER,
                            params=NamedEntityProviderParams(
                                provider=NamedEntityProvider.ADDRESS_CITY,
                                locale="en_us",
                                seed=5,
                            ),
                        ),
                    ),
                    Column(
                        name="ssn",
                        data_type="string",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.NAMED_ENTITY_PROVIDER,
                            params=NamedEntityProviderParams(
                                provider=NamedEntityProvider.SSN,
                                locale="en_us",
                                unique_values=1000,
                                with_replacement=False,
                                seed=6,
                            ),
                        ),
                    ),
                ],
            ),
        ],
    )

    customers_generate = ra.GenerateFromDataSchema(
        ra.GenerateFromDataSchema.Config(schema=customers_schema, upload_datasets=True)
    )

    builder = rf.WorkflowBuilder()
    builder.add(customers_generate)
    workflow = await builder.start(conn)
    print(f"Workflow ID: {workflow.id()}")
    await workflow.wait(raise_on_failure=True)

    customers_remote = await workflow.datasets().nth(0)
    customers_dataset = await customers_remote.to_local(conn)
    customers_df = customers_dataset.to_pandas()
    print(customers_df.head(10))

    print(f"Total rows: {len(customers_df)}")
    print(f"Unique emails: {customers_df['email'].nunique()}")
    print(f"Unique SSNs: {customers_df['ssn'].nunique()}")

    # Multilingual variant: switching `locale` changes the language/region
    # of generated values. For example, locale="de" yields German names
    # and cities.
    german_customers_schema = DataSchema(
        entities=[
            Entity(
                name="customers_de",
                cardinality=10,
                columns=[
                    Column(
                        name="customer_id",
                        data_type="string",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.ID,
                            params=IDParams(template_str="CUST_DE_{id}"),
                        ),
                    ),
                    Column(
                        name="full_name",
                        data_type="string",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.NAMED_ENTITY_PROVIDER,
                            params=NamedEntityProviderParams(
                                provider=NamedEntityProvider.PERSON_FULL_NAME,
                                locale="de",
                                seed=10,
                            ),
                        ),
                    ),
                    Column(
                        name="city",
                        data_type="string",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.NAMED_ENTITY_PROVIDER,
                            params=NamedEntityProviderParams(
                                provider=NamedEntityProvider.ADDRESS_CITY,
                                locale="de",
                                seed=11,
                            ),
                        ),
                    ),
                ],
            ),
        ],
    )

    de_generate = ra.GenerateFromDataSchema(
        ra.GenerateFromDataSchema.Config(
            schema=german_customers_schema, upload_datasets=True
        )
    )
    builder = rf.WorkflowBuilder()
    builder.add(de_generate)
    workflow = await builder.start(conn)
    await workflow.wait(raise_on_failure=True)
    de_remote = await workflow.datasets().nth(0)
    de_dataset = await de_remote.to_local(conn)
    print(de_dataset.to_pandas())


EXAMPLES = {
    1: ("Simple device schema (JSON dict)",                           example_1_device_schema),
    2: ("User sessions with state machine and timeseries",            example_2_user_sessions_state_machine),
    3: ("Trades with composite foreign key",                          example_3_trades_composite_fk),
    4: ("Customers via NamedEntityProvider (+ multilingual variant)", example_4_named_entity_provider),
}


async def main(example_numbers: list[int]) -> None:
    async with rf.Connection.from_config() as conn:
        for n in example_numbers:
            title, func = EXAMPLES[n]
            banner = f" Example {n}: {title} "
            print(f"\n{'=' * 72}\n{banner.center(72, '=')}\n{'=' * 72}\n")
            await func(conn)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate synthetic data from schema specifications.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-e", "--example",
        type=int,
        action="append",
        choices=[1, 2, 3, 4],
        help="Run a specific example (1-4). Repeatable. Default: run all four.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    examples_to_run = args.example or list(EXAMPLES.keys())
    asyncio.run(main(examples_to_run))
