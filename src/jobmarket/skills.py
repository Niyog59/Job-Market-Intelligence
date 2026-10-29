"""Deterministic clean-up of skill names returned by requirement extraction.

Only exact aliases are merged: a name is lowercased, surrounding punctuation and repeated
spaces are removed, and the result is looked up in ALIASES. Nothing is matched by
substring or similarity, so "Java" never becomes "JavaScript", "Spark" never becomes
"PySpark", and an unknown name is kept as written (marked unmapped) rather than guessed.
Related skills are never added: "Snowflake" does not imply "SQL".

The alias table is versioned with NORMALIZATION_VERSION in jobmarket.extraction; changing
it means re-normalizing stored results (no new model calls).
"""

import re

# canonical name -> aliases (lowercase). The canonical name is matched too.
_CANONICAL: dict[str, list[str]] = {
    "SQL": ["sql"],
    "PostgreSQL": [
        "postgres",
        "postgresql",
        "postgre sql",
        "psql",
        "postgres db",
        "postgresql db",
        "postgres database",
        "postgresql database",
    ],
    "MySQL": ["mysql", "my sql"],
    "SQL Server": ["sql server", "microsoft sql server", "ms sql server", "mssql", "ms sql"],
    "T-SQL": ["t-sql", "tsql", "transact-sql"],
    "PL/SQL": ["pl/sql", "plsql"],
    "Oracle Database": ["oracle database", "oracle db"],
    "MongoDB": ["mongodb", "mongo"],
    "Redis": ["redis"],
    "Cassandra": ["cassandra", "apache cassandra"],
    "DynamoDB": ["dynamodb", "amazon dynamodb", "aws dynamodb"],
    "Elasticsearch": ["elasticsearch", "elastic search"],
    "Snowflake": ["snowflake"],
    "BigQuery": ["bigquery", "google bigquery", "big query"],
    "Redshift": ["redshift", "amazon redshift", "aws redshift"],
    "Databricks": ["databricks"],
    "Python": ["python", "python3", "python 3"],
    "Java": ["java"],
    "JavaScript": ["javascript", "js", "ecmascript"],
    "TypeScript": ["typescript", "ts"],
    "C#": ["c#", "c sharp", "csharp"],
    "C++": ["c++", "cpp"],
    "C": ["c"],
    "Go": ["golang", "go lang"],
    "Rust": ["rust"],
    "Ruby": ["ruby"],
    "Ruby on Rails": ["ruby on rails", "rails", "ror"],
    "PHP": ["php"],
    "Scala": ["scala"],
    "Kotlin": ["kotlin"],
    "Swift": ["swift"],
    "R": ["r"],
    "SAS": ["sas"],
    "MATLAB": ["matlab"],
    "Bash": ["bash", "shell scripting", "bash scripting"],
    ".NET": [".net", "dotnet", "dot net", ".net core", "asp.net", "asp.net core"],
    "Node.js": ["node.js", "nodejs", "node js", "node"],
    "React": ["react", "react.js", "reactjs"],
    "Angular": ["angular", "angularjs", "angular.js"],
    "Vue.js": ["vue", "vue.js", "vuejs"],
    "Django": ["django"],
    "Flask": ["flask"],
    "FastAPI": ["fastapi", "fast api"],
    "Spring Boot": ["spring boot", "springboot"],
    "Spring": ["spring", "spring framework"],
    "GraphQL": ["graphql"],
    "REST APIs": ["rest", "rest api", "rest apis", "restful", "restful api", "restful apis"],
    "gRPC": ["grpc"],
    "Apache Spark": ["spark", "apache spark"],
    "PySpark": ["pyspark"],
    "Apache Kafka": ["kafka", "apache kafka"],
    "Apache Airflow": ["airflow", "apache airflow"],
    "Apache Flink": ["flink", "apache flink"],
    "Hadoop": ["hadoop", "apache hadoop"],
    "Hive": ["hive", "apache hive"],
    "dbt": ["dbt", "data build tool", "dbt core", "dbt cloud"],
    "Informatica": ["informatica"],
    "SSIS": ["ssis", "sql server integration services"],
    "SSRS": ["ssrs", "sql server reporting services"],
    "SSAS": ["ssas", "sql server analysis services"],
    "Fivetran": ["fivetran"],
    "Pandas": ["pandas"],
    "NumPy": ["numpy"],
    "scikit-learn": ["scikit-learn", "sklearn", "scikit learn"],
    "TensorFlow": ["tensorflow"],
    "PyTorch": ["pytorch", "torch"],
    "Tableau": ["tableau"],
    "Power BI": ["power bi", "powerbi", "microsoft power bi"],
    "Looker": ["looker"],
    "Qlik": ["qlik", "qlikview", "qlik sense"],
    "Excel": ["excel", "microsoft excel", "ms excel"],
    "AWS": ["aws", "amazon web services", "amazon aws"],
    "Microsoft Azure": ["azure", "microsoft azure"],
    "Google Cloud": ["gcp", "google cloud", "google cloud platform"],
    "Docker": ["docker"],
    "Kubernetes": ["kubernetes", "k8s"],
    "Terraform": ["terraform"],
    "Git": ["git"],
    "GitHub": ["github"],
    "Linux": ["linux"],
    "CI/CD": ["ci/cd", "cicd", "ci cd", "continuous integration", "continuous delivery"],
    "Jenkins": ["jenkins"],
    "Microservices": ["microservices", "microservice architecture"],
    "Machine learning": ["machine learning", "ml"],
    "Statistics": ["statistics", "statistical analysis"],
    "Data modeling": ["data modeling", "data modelling"],
    "ETL": ["etl", "extract transform load"],
    "ELT": ["elt"],
}

ALIASES: dict[str, str] = {}
for _canonical, _aliases in _CANONICAL.items():
    for _alias in [_canonical.lower(), *_aliases]:
        existing = ALIASES.setdefault(_alias, _canonical)
        if existing != _canonical:  # pragma: no cover - guards the table itself
            raise ValueError(f"alias {_alias!r} maps to both {existing!r} and {_canonical!r}")

_EDGES = re.compile(r"^[\s\"'`,;:()\[\]{}]+|[\s\"'`,;:()\[\]{}]+$")
_SPACES = re.compile(r"\s+")


def clean_key(name: str) -> str:
    key = _SPACES.sub(" ", name).strip()
    key = _EDGES.sub("", key)
    # A leading dot is meaningful in ".NET"; a trailing one is sentence punctuation.
    if key.endswith(".") and key.lower() not in ALIASES:
        key = key.rstrip(".")
    return key.lower()


def canonical_skill(name: str) -> tuple[str, bool]:
    """(display name, mapped). Unknown names are returned as written, tidied of stray
    punctuation and spaces, with mapped False."""
    key = clean_key(name)
    if key in ALIASES:
        return ALIASES[key], True
    tidy = _EDGES.sub("", _SPACES.sub(" ", name).strip())
    return tidy or name.strip(), False
