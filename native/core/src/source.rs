use serde::de::{Deserializer, Error, MapAccess, Visitor};
use serde_json::{Map, Value};
use std::fmt;

pub const MAX_CONTEXT_BYTES: usize = 1_000_000;

pub fn parse_source(text: &str) -> Result<Value, &'static str> {
    if text.len() > MAX_CONTEXT_BYTES {
        return Err("Semantic source exceeds the 1 MB context limit");
    }
    struct SourceRow;
    impl<'de> Visitor<'de> for SourceRow {
        type Value = Value;

        fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
            formatter.write_str("a source row with unique column names")
        }

        fn visit_map<A: MapAccess<'de>>(self, mut entries: A) -> Result<Value, A::Error> {
            let mut fields = Map::new();
            while let Some((key, value)) = entries.next_entry::<String, Value>()? {
                if fields.insert(key, value).is_some() {
                    return Err(A::Error::custom("Duplicate source column"));
                }
            }
            Ok(Value::Object(fields))
        }
    }
    let mut decoder = serde_json::Deserializer::from_str(text);
    let row = decoder
        .deserialize_map(SourceRow)
        .map_err(|_| "Source must have unique column names and valid JSON values")?;
    decoder.end().map_err(|_| "Invalid source JSON")?;
    Ok(row)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn source_keeps_exact_numbers_and_rejects_duplicate_names() {
        let row =
            parse_source(r#"{"金额":900719925474099312345.123456789,"missing":null}"#).unwrap();
        assert_eq!(row["金额"].to_string(), "900719925474099312345.123456789");
        assert!(row["missing"].is_null());
        assert!(parse_source(r#"{"x":1,"x":2}"#).is_err());
        assert!(parse_source("[]").is_err());
        assert!(parse_source(&" ".repeat(MAX_CONTEXT_BYTES + 1)).is_err());
    }
}
