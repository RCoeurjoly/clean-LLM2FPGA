use std::{collections::BTreeMap, fmt::Write as _};

use serde::{Deserialize, Serialize};
use thiserror::Error;

use crate::{parse_ground_smt, GroundSmtError, VcArtifact, VcKind};

pub const LEAN_AUDIT_SCHEMA_VERSION: u32 = 1;

/// Exact checker/toolchain identity used to accept a generated Lean audit module.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LeanCheckerFingerprint {
    pub lean_version: String,
    pub lake_version: String,
    pub lean_binary_blake3: String,
    pub lake_binary_blake3: String,
    pub lean_toolchain_blake3: String,
    pub lake_manifest_blake3: String,
}

impl LeanCheckerFingerprint {
    pub fn digest(&self) -> String {
        pceq_core::canonical_hash("pceq/lean-checker-fingerprint/v1", self)
            .expect("Lean checker fingerprint canonical serialization")
            .to_string()
    }

    pub fn validate(&self) -> Result<(), LeanAuditError> {
        if self.lean_version.trim().is_empty() || self.lake_version.trim().is_empty() {
            return Err(LeanAuditError::EmptyCheckerVersion);
        }
        for (field, digest) in [
            ("lean_binary_blake3", self.lean_binary_blake3.as_str()),
            ("lake_binary_blake3", self.lake_binary_blake3.as_str()),
            ("lean_toolchain_blake3", self.lean_toolchain_blake3.as_str()),
            ("lake_manifest_blake3", self.lake_manifest_blake3.as_str()),
        ] {
            if !is_digest(digest) {
                return Err(LeanAuditError::InvalidDigest(field.into()));
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LeanVcManifestEntry {
    pub vc_digest: String,
    pub definition: String,
    pub theorem: String,
    pub kind: VcKind,
    pub product_hash: String,
    pub certificate_hash: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LeanAuditModule {
    pub namespace: String,
    pub source: String,
    pub manifest: Vec<LeanVcManifestEntry>,
}

/// Content-addressed evidence that one exact generated module was accepted by
/// one exact Lean checker/toolchain identity.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LeanAuditArtifact {
    pub schema_version: u32,
    pub checker: LeanCheckerFingerprint,
    pub module: LeanAuditModule,
}

impl LeanAuditArtifact {
    pub fn new(
        checker: LeanCheckerFingerprint,
        module: LeanAuditModule,
    ) -> Result<Self, LeanAuditError> {
        let artifact = Self {
            schema_version: LEAN_AUDIT_SCHEMA_VERSION,
            checker,
            module,
        };
        artifact.validate()?;
        Ok(artifact)
    }

    pub fn checker_hash(&self) -> String {
        self.checker.digest()
    }

    pub fn validate(&self) -> Result<(), LeanAuditError> {
        if self.schema_version != LEAN_AUDIT_SCHEMA_VERSION {
            return Err(LeanAuditError::Schema(self.schema_version));
        }
        self.checker.validate()?;
        validate_namespace(&self.module.namespace)
            .map_err(|_| LeanAuditError::InvalidNamespace(self.module.namespace.clone()))?;
        if self.module.source.trim().is_empty() || self.module.manifest.is_empty() {
            return Err(LeanAuditError::EmptyModule);
        }
        let mut previous = None;
        for entry in &self.module.manifest {
            if !is_digest(&entry.vc_digest) {
                return Err(LeanAuditError::InvalidDigest("vc_digest".into()));
            }
            if previous
                .as_ref()
                .is_some_and(|digest: &&String| *digest >= &entry.vc_digest)
            {
                return Err(LeanAuditError::ManifestOrder);
            }
            let expected_definition = format!("vc_{}_value", entry.vc_digest);
            let expected_theorem = format!("vc_{}_accepted", entry.vc_digest);
            if entry.definition != expected_definition || entry.theorem != expected_theorem {
                return Err(LeanAuditError::ManifestIdentifier(entry.vc_digest.clone()));
            }
            if entry.product_hash.trim().is_empty() || entry.certificate_hash.trim().is_empty() {
                return Err(LeanAuditError::EmptyManifestLink(entry.vc_digest.clone()));
            }
            if !self.module.source.contains(&format!(
                "theorem {} : {} = true := by native_decide",
                entry.theorem, entry.definition
            )) {
                return Err(LeanAuditError::MissingTheorem(entry.vc_digest.clone()));
            }
            previous = Some(&entry.vc_digest);
        }
        Ok(())
    }
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum LeanAuditError {
    #[error("unsupported Lean audit schema version {0}")]
    Schema(u32),
    #[error("Lean checker version fields must not be empty")]
    EmptyCheckerVersion,
    #[error("invalid Lean audit digest field {0}")]
    InvalidDigest(String),
    #[error("invalid Lean audit namespace {0:?}")]
    InvalidNamespace(String),
    #[error("Lean audit source and manifest must not be empty")]
    EmptyModule,
    #[error("Lean audit manifest must be strictly digest-sorted without duplicates")]
    ManifestOrder,
    #[error("Lean audit manifest has a noncanonical identifier for VC {0}")]
    ManifestIdentifier(String),
    #[error("Lean audit manifest has an empty product/certificate link for VC {0}")]
    EmptyManifestLink(String),
    #[error("Lean audit source omits the manifest theorem for VC {0}")]
    MissingTheorem(String),
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum LeanExportError {
    #[error("invalid Lean namespace {0:?}")]
    InvalidNamespace(String),
    #[error("cannot export an empty VC list")]
    Empty,
    #[error("duplicate VC digest {0}")]
    DuplicateDigest(String),
    #[error("VC {digest} is structurally invalid: {reason}")]
    InvalidArtifact { digest: String, reason: String },
    #[error("VC {digest} has a non-comment SMT declaration at line {line}")]
    UnsupportedDeclaration { digest: String, line: usize },
    #[error("VC {digest} is outside the ground audit fragment: {source}")]
    Formula {
        digest: String,
        #[source]
        source: GroundSmtError,
    },
}

/// Parse, transpile, and canonically order a batch of exact VC artifacts.
/// The returned manifest and every generated Lean identifier are linked to the
/// full canonical VC digest. No caller-supplied proof status is accepted.
pub fn export_ground_vc_module(
    namespace: &str,
    vcs: &[VcArtifact],
) -> Result<LeanAuditModule, LeanExportError> {
    validate_namespace(namespace)?;
    if vcs.is_empty() {
        return Err(LeanExportError::Empty);
    }

    let mut parsed = BTreeMap::new();
    for vc in vcs {
        let digest = vc.digest();
        vc.validate()
            .map_err(|error| LeanExportError::InvalidArtifact {
                digest: digest.clone(),
                reason: error.to_string(),
            })?;
        validate_comment_declarations(&digest, &vc.declarations)?;
        let expression =
            parse_ground_smt(&vc.formula).map_err(|source| LeanExportError::Formula {
                digest: digest.clone(),
                source,
            })?;
        if parsed.insert(digest.clone(), (vc, expression)).is_some() {
            return Err(LeanExportError::DuplicateDigest(digest));
        }
    }

    let mut source = format!(
        "import Std.Tactic.BVDecide\n\nnamespace {namespace}\n\n-- Generated from exact canonical PCEQ VC digests.\n"
    );
    let mut manifest = Vec::with_capacity(parsed.len());
    for (digest, (vc, expression)) in parsed {
        let definition = format!("vc_{digest}_value");
        let theorem = format!("vc_{digest}_accepted");
        write!(
            source,
            "\n-- VC digest: {digest}\ndef {definition} : Bool :=\n  {}\n\ntheorem {theorem} : {definition} = true := by native_decide\n",
            expression.to_lean_bool()
        )
        .expect("writing to String cannot fail");
        manifest.push(LeanVcManifestEntry {
            vc_digest: digest,
            definition,
            theorem,
            kind: vc.kind,
            product_hash: vc.product_hash.clone(),
            certificate_hash: vc.certificate_hash.clone(),
        });
    }
    writeln!(source, "\nend {namespace}").expect("writing to String cannot fail");
    Ok(LeanAuditModule {
        namespace: namespace.into(),
        source,
        manifest,
    })
}

fn validate_namespace(namespace: &str) -> Result<(), LeanExportError> {
    let valid = !namespace.is_empty()
        && namespace.split('.').all(|part| {
            let mut characters = part.chars();
            characters
                .next()
                .is_some_and(|first| first.is_ascii_uppercase())
                && characters.all(|character| character.is_ascii_alphanumeric() || character == '_')
        });
    if valid {
        Ok(())
    } else {
        Err(LeanExportError::InvalidNamespace(namespace.into()))
    }
}

fn validate_comment_declarations(digest: &str, declarations: &str) -> Result<(), LeanExportError> {
    for (index, line) in declarations.lines().enumerate() {
        let line = line.trim();
        if !line.is_empty() && !line.starts_with(';') {
            return Err(LeanExportError::UnsupportedDeclaration {
                digest: digest.into(),
                line: index + 1,
            });
        }
    }
    Ok(())
}

fn is_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;

    use crate::{
        FiniteEdgeContext, FiniteTransitionCase, VcArtifact, VcGenerator, VcKind, VC_SCHEMA_VERSION,
    };

    use super::*;

    fn vc(kind: VcKind, formula: &str, owner: &str) -> VcArtifact {
        VcArtifact {
            kind,
            declarations: "; finite-domain replay".into(),
            formula: formula.into(),
            product_hash: "a".repeat(64),
            certificate_hash: owner.into(),
            schema_version: VC_SCHEMA_VERSION,
        }
    }

    #[test]
    fn renders_one_digest_linked_definition_and_theorem_per_vc() {
        let first = vc(VcKind::InitialSafety, "true", "global");
        let second = vc(
            VcKind::EdgeConsecution,
            "(and (= (_ bv3 2) (_ bv3 2)) (not false))",
            &"b".repeat(64),
        );
        let output = export_ground_vc_module("Pceq.Generated", &[first.clone(), second.clone()])
            .expect("export");
        assert_eq!(output.manifest.len(), 2);
        for artifact in [&first, &second] {
            let digest = artifact.digest();
            assert!(output.source.contains(&format!("-- VC digest: {digest}")));
            assert!(output
                .source
                .contains(&format!("def vc_{digest}_value : Bool")));
            assert!(output.source.contains(&format!(
                "theorem vc_{digest}_accepted : vc_{digest}_value = true := by native_decide"
            )));
        }
        assert!(output.source.contains("BitVec.ofNat 2 3"));
    }

    #[test]
    fn exact_finite_edge_generator_is_inside_the_audit_fragment() {
        let source_states = BTreeSet::from([0, 2]);
        let reachable_states = BTreeSet::from([0, 1, 2]);
        let bad_states = BTreeSet::new();
        let post_states = BTreeSet::from([1, 2]);
        let cases = [
            FiniteTransitionCase {
                transition_index: 0,
                source: 0,
                target: 1,
            },
            FiniteTransitionCase {
                transition_index: 1,
                source: 2,
                target: 2,
            },
        ];
        let context = FiniteEdgeContext {
            product_hash: "a",
            certificate_hash: "b",
            source_states: &source_states,
            reachable_states: &reachable_states,
            bad_states: &bad_states,
            post_states: &post_states,
            cases: &cases,
        };
        let vcs = VcGenerator.edge(&context).expect("finite VCs");
        let output = export_ground_vc_module("Pceq.Generated", &vcs).expect("Lean export");
        assert_eq!(output.manifest.len(), VcKind::REQUIRED_EDGE.len());
        assert_eq!(output.source.matches("by native_decide").count(), 4);
        for vc in &vcs {
            assert!(output
                .manifest
                .iter()
                .any(|entry| entry.vc_digest == vc.digest()));
        }
    }

    #[test]
    fn output_is_deterministic_and_order_independent() {
        let first = vc(VcKind::InitialSafety, "true", "global");
        let second = vc(VcKind::NonBlocking, "(or false true)", "node:a");
        let forward = export_ground_vc_module("Pceq.Generated", &[first.clone(), second.clone()])
            .expect("forward");
        let reverse = export_ground_vc_module("Pceq.Generated", &[second, first]).expect("reverse");
        assert_eq!(forward, reverse);
    }

    #[test]
    fn tamper_changes_digest_linkage_and_unsupported_tamper_is_rejected() {
        let original = vc(VcKind::InitialSafety, "true", "global");
        let original_output =
            export_ground_vc_module("Audit", std::slice::from_ref(&original)).expect("export");
        let mut changed = original.clone();
        changed.formula = "(not false)".into();
        let changed_output =
            export_ground_vc_module("Audit", std::slice::from_ref(&changed)).expect("export");
        assert_ne!(original.digest(), changed.digest());
        assert_ne!(original_output.source, changed_output.source);
        assert_eq!(changed_output.manifest[0].vc_digest, changed.digest());

        changed.formula = "(=> free true)".into();
        assert!(matches!(
            export_ground_vc_module("Audit", &[changed]),
            Err(LeanExportError::Formula { .. })
        ));
    }

    #[test]
    fn rejects_duplicate_vcs_declarations_and_invalid_namespaces() {
        let artifact = vc(VcKind::InitialSafety, "true", "global");
        assert!(matches!(
            export_ground_vc_module("Audit", &[artifact.clone(), artifact.clone()]),
            Err(LeanExportError::DuplicateDigest(_))
        ));
        let mut declared = artifact.clone();
        declared.declarations = "(declare-const p Bool)".into();
        assert!(matches!(
            export_ground_vc_module("Audit", &[declared]),
            Err(LeanExportError::UnsupportedDeclaration { .. })
        ));
        for namespace in ["", "audit", "Pceq.bad", "Pceq-Generated", "1Audit"] {
            assert!(matches!(
                export_ground_vc_module(namespace, std::slice::from_ref(&artifact)),
                Err(LeanExportError::InvalidNamespace(_))
            ));
        }
        assert!(matches!(
            export_ground_vc_module("Audit", &[]),
            Err(LeanExportError::Empty)
        ));
    }

    #[test]
    fn rejects_width_and_syntax_errors_before_rendering() {
        for formula in [
            "(= (_ bv2 1) (_ bv0 1))",
            "(= (_ bv0 1) (_ bv0 2))",
            "(and true)",
            "(xor true false)",
            "true trailing",
        ] {
            let artifact = vc(VcKind::EdgeSafety, formula, &"c".repeat(64));
            assert!(matches!(
                export_ground_vc_module("Audit", &[artifact]),
                Err(LeanExportError::Formula { .. })
            ));
        }
    }
}
