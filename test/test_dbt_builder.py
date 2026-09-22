from agents.dbt_builder.app import validate_project


def project(sql: str = "select id, status from {{ source('raw', 'orders') }}") -> dict[str, str]:
    return {
        "dbt_project.yml": "name: aqe_analytics\nversion: 1.0.0\nprofile: aqe_generated\n",
        "models/sources.yml": "version: 2\nsources:\n  - name: raw\n    tables:\n      - name: orders\n",
        "models/schema.yml": "version: 2\nmodels:\n  - name: fct_orders\n    columns:\n      - name: id\n        tests: [unique, not_null]\n",
        "models/staging/stg_orders.sql": sql,
        "models/intermediate/int_orders.sql": "select * from {{ ref('stg_orders') }}",
        "models/marts/fct_orders.sql": "select * from {{ ref('int_orders') }}",
        "README.md": "# AQE analytics\n",
    }


def test_valid_medallion_project_passes_static_validation():
    errors = validate_project(project(), "snowflake")

    assert errors == []


def test_write_statement_is_rejected():
    errors = validate_project(project("delete from raw.orders"), "snowflake")

    assert any("read-only" in error for error in errors)


def test_missing_data_tests_are_rejected():
    files = project()
    files["models/schema.yml"] = "version: 2\nmodels: []\n"

    assert any("unique and not_null" in error for error in validate_project(files, "snowflake"))
