//! Separately committed evidence and request ownership. Credentials never enter receipts.

use crate::evidence::Observation;
use serde::Deserialize;
use serde_json::Value;
use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;
use tokio::task::JoinHandle;
use tokio_postgres::{Client, Config, NoTls, config::Host};

#[derive(Clone, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct RegistryConfig {
    pub dsn: String,
    pub max_active: i32,
    pub max_daily: i32,
    pub max_age_seconds: i32,
    pub connect_timeout_ms: u64,
}

impl Default for RegistryConfig {
    fn default() -> Self {
        Self {
            dsn: String::new(),
            max_active: 16,
            max_daily: 10_000,
            max_age_seconds: 86_400,
            connect_timeout_ms: 2_000,
        }
    }
}

#[derive(Clone, Deserialize)]
pub struct SavedObservation {
    pub attempt: String,
    pub observation: Observation,
}

#[derive(Deserialize)]
pub struct Claim {
    pub state: String,
    pub attempt: Option<String>,
    pub observation: Option<Observation>,
}

pub struct Registry {
    config: RegistryConfig,
    connection: Config,
    scope: String,
    pool: String,
    client: Option<Arc<Client>>,
    driver: Option<JoinHandle<()>>,
}

impl Registry {
    pub fn new(config: RegistryConfig, scope: String, pool: String) -> Result<Self, &'static str> {
        if !(1..=128).contains(&config.max_active)
            || !(0..=1_000_000).contains(&config.max_daily)
            || !(0..=31_536_000).contains(&config.max_age_seconds)
            || !(100..=10_000).contains(&config.connect_timeout_ms)
            || scope.is_empty()
            || scope.len() > 4096
        {
            return Err("Invalid native registry limits");
        }
        let mut connection: Config = config
            .dsn
            .parse()
            .map_err(|_| "Invalid registry connection")?;
        if connection.get_user().is_none()
            || connection.get_dbname().is_none()
            || connection.get_hosts().is_empty()
            || !connection
                .get_hostaddrs()
                .iter()
                .all(|address| address.is_loopback())
            || !connection.get_hosts().iter().all(|host| match host {
                Host::Tcp(name) => name
                    .parse::<std::net::IpAddr>()
                    .is_ok_and(|ip| ip.is_loopback()),
                #[cfg(unix)]
                Host::Unix(_) => true,
            })
        {
            return Err(
                "Registry requires an explicit local PostgreSQL socket or loopback address, database and restricted login",
            );
        }
        connection.application_name("jev-native-registry");
        connection.options("-c statement_timeout=2000 -c lock_timeout=500 -c idle_in_transaction_session_timeout=5000");
        Ok(Self {
            config,
            connection,
            scope,
            pool,
            client: None,
            driver: None,
        })
    }

    pub async fn connect(&mut self) -> Result<(), &'static str> {
        if self
            .client
            .as_ref()
            .is_some_and(|client| !client.is_closed())
        {
            return Ok(());
        }
        if let Some(driver) = self.driver.take() {
            driver.abort();
        }
        let (client, connection) = tokio::time::timeout(
            Duration::from_millis(self.config.connect_timeout_ms),
            self.connection.connect(NoTls),
        )
        .await
        .map_err(|_| "Registry connection timed out")?
        .map_err(|_| "Registry connection unavailable")?;
        self.driver = Some(tokio::spawn(async move {
            let _ = connection.await;
        }));
        self.client = Some(Arc::new(client));
        Ok(())
    }

    pub async fn lookup(
        &mut self,
        identities: &[String],
    ) -> Result<HashMap<String, SavedObservation>, &'static str> {
        if identities.is_empty() {
            return Ok(HashMap::new());
        }
        self.connect().await?;
        let row = self
            .client
            .as_ref()
            .unwrap()
            .query_one(
                "SELECT jev_native._registry_lookup($1,$2,$3)",
                &[&self.scope, &identities, &self.config.max_age_seconds],
            )
            .await
            .map_err(|_| "Registry evidence lookup failed")?;
        let values: Value = row.get(0);
        let entries = values
            .as_array()
            .ok_or("Invalid registry evidence result")?;
        let mut results = HashMap::new();
        for entry in entries {
            let identity = entry["identity"]
                .as_str()
                .ok_or("Missing evidence identity")?;
            let saved =
                serde_json::from_value(entry.clone()).map_err(|_| "Invalid stored observation")?;
            results.insert(identity.to_owned(), saved);
        }
        Ok(results)
    }

    pub async fn claim(&self, identity: &str, timeout_ms: u64) -> Result<Claim, &'static str> {
        let client = self.client.as_ref().ok_or("Registry is not connected")?;
        let deadline = timeout_ms as i32 + 10_000;
        let row = client
            .query_one(
                "SELECT jev_native._registry_claim($1,$2,$3,$4,$5,$6,$7,false)",
                &[
                    &self.scope,
                    &identity,
                    &self.pool,
                    &self.config.max_active,
                    &self.config.max_daily,
                    &deadline,
                    &self.config.max_age_seconds,
                ],
            )
            .await
            .map_err(|_| "Registry claim outcome is unavailable")?;
        serde_json::from_value(row.get(0)).map_err(|_| "Invalid registry claim result")
    }

    pub async fn finish(
        &self,
        attempt: &str,
        observation: Option<&Observation>,
        failure: Option<&str>,
    ) -> Result<bool, &'static str> {
        let client = self.client.as_ref().ok_or("Registry is not connected")?;
        let observation = observation
            .map(serde_json::to_value)
            .transpose()
            .map_err(|_| "Invalid observation")?;
        client
            .query_one(
                "SELECT jev_native._registry_finish($1::text::uuid,$2,$3)",
                &[&attempt, &observation, &failure],
            )
            .await
            .map(|row| row.get(0))
            .map_err(|_| "Evidence publication was not confirmed")
    }
}

impl Drop for Registry {
    fn drop(&mut self) {
        if let Some(driver) = self.driver.take() {
            driver.abort();
        }
    }
}
