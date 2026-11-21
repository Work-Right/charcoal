//! The eval's model registry. Eval-only by design: nothing here is reachable
//! from production `build_from_env`. Part B adds jev, clef-flash, clef and
//! strands-decider here.

use anyhow::{bail, Context, Result};
use std::sync::Arc;

use crate::toxicity::classifier::ToxicityClassifier;
use crate::toxicity::cost_meter::ScanCostMeter;
use crate::toxicity::detoxify_classifier::DetoxifyClassifier;
use crate::toxicity::onnx::OnnxToxicityScorer;
use crate::toxicity::runpod_cope_b::{RunPodCopeBClient, COPE_B_DEFAULT_POLICY, COPE_B_POLICY_ENV};

/// #437: CoPE-B served locally on the Mac mini (gpu/cope-b-local/server.py).
pub const LOCAL_EVAL_MODEL: &str = "local-cope-b";

pub const KNOWN_MODELS: &[&str] = &["cope-b", "detoxify", LOCAL_EVAL_MODEL];

/// The run's cap as the meter's ceiling. Never 0: a zero ceiling disables
/// the backstop entirely.
pub fn cap_cents(cap_usd: f64) -> u32 {
    ((cap_usd * 100.0).ceil() as u32).max(1)
}

/// Refuse a missing/default CoPE-B policy version. Pure (takes the raw value)
/// so it is testable without touching process-global env.
///
/// Why before the client exists: with the variable unset the client expects
/// "policy-unknown", and the mismatch used to be detected only AFTER the
/// probe's paid warm-up woke the GPU. That refuse-after-wake pattern cost
/// $193 in 16 days in production; the eval must fail before any network call.
fn require_cope_b_policy(raw: Option<&str>) -> Result<()> {
    match raw.map(str::trim) {
        None | Some("") => bail!(
            "{COPE_B_POLICY_ENV} must be set for cope-b: without it every verdict is \
             refused only after a paid GPU wake. Refusing before any network call."
        ),
        Some(v) if v == COPE_B_DEFAULT_POLICY => bail!(
            "{COPE_B_POLICY_ENV} is the placeholder {COPE_B_DEFAULT_POLICY:?}; set the real \
             policy version. Refusing before any network call."
        ),
        Some(_) => Ok(()),
    }
}

pub fn build_eval_model(name: &str, cap_usd: f64) -> Result<Arc<dyn ToxicityClassifier>> {
    match name {
        // Not via `build_backend_named`, which deliberately builds the client
        // with a DISABLED meter. Here the production cost meter (real GPU
        // worker time x rate) is set to the run's cap, so CoPE-B's spend is
        // bounded by what RunPod actually ran, not by an estimate.
        "cope-b" => {
            require_cope_b_policy(std::env::var(COPE_B_POLICY_ENV).ok().as_deref())?;
            let endpoint = std::env::var("RUNPOD_ENDPOINT_URL")
                .context("RUNPOD_ENDPOINT_URL must be set for cope-b")?;
            let api_key =
                std::env::var("RUNPOD_API_KEY").context("RUNPOD_API_KEY must be set for cope-b")?;
            // Same rate/workers resolution as production (`from_env`), with
            // the run cap as ceiling. The meter is a pre-call check: an
            // in-flight batch completes and RunPod's ~60 s idle tail per
            // worker is not counted, so real spend can overshoot the cap by
            // about one batch plus one tail.
            let meter = Arc::new(ScanCostMeter::from_env_with_ceiling(cap_cents(cap_usd)));
            Ok(Arc::new(
                RunPodCopeBClient::new(endpoint, api_key)?.with_meter(meter),
            ))
        }
        LOCAL_EVAL_MODEL => build_local_cope_b(
            std::env::var(COPE_B_POLICY_ENV).ok().as_deref(),
            std::env::var(crate::toxicity::runpod_cope_b::LOCAL_COPE_B_URL_ENV)
                .ok()
                .as_deref(),
        ),
        "detoxify" => {
            let dir = crate::toxicity::download::resolve_model_dir();
            Ok(Arc::new(DetoxifyClassifier::new(OnnxToxicityScorer::load(
                &dir,
            )?)))
        }
        other => bail!(
            "unknown eval model {other:?} (known: {})",
            KNOWN_MODELS.join(", ")
        ),
    }
}

/// The local Mac mini model. Free, so no cost meter, but the same
/// policy-version refusal as `cope-b`: the server reports its policy on every
/// verdict, and scoring a server on the wrong policy would compare the wrong
/// thing. Takes raw values so it is testable without process-global env.
fn build_local_cope_b(
    policy_raw: Option<&str>,
    url_raw: Option<&str>,
) -> Result<Arc<dyn ToxicityClassifier>> {
    require_cope_b_policy(policy_raw)?;
    let url = crate::toxicity::runpod_cope_b::local_cope_b_url(url_raw);
    Ok(Arc::new(RunPodCopeBClient::local(url)?))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The $193 incident: an unset policy var makes the client expect
    /// "policy-unknown", and the refusal only came after a paid GPU wake.
    #[test]
    fn missing_or_default_policy_is_refused_before_any_network_call() {
        for raw in [
            None,
            Some(""),
            Some("   "),
            Some("policy-unknown"),
            Some(" policy-unknown\n"),
        ] {
            let err = require_cope_b_policy(raw).expect_err("must refuse");
            assert!(
                format!("{err:#}").contains("CHARCOAL_COPE_B_POLICY_VERSION"),
                "{raw:?}"
            );
        }
        assert!(require_cope_b_policy(Some("policy-ddac468-2026-07-03")).is_ok());
    }

    /// #437: the local Mac mini model is evaluable, priced (free), and keeps
    /// the policy refusal: a server on the wrong policy must not be scored.
    #[test]
    fn local_cope_b_is_known_priced_and_policy_checked() {
        assert!(KNOWN_MODELS.contains(&LOCAL_EVAL_MODEL));
        let prices = crate::cli::eval_classifiers::cost::load_pricing(
            &std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("evals/classifiers/pricing.json"),
        )
        .expect("pricing.json loads");
        let price = crate::cli::eval_classifiers::cost::price_for(&prices, LOCAL_EVAL_MODEL)
            .expect("local model is priced");
        assert_eq!(
            crate::cli::eval_classifiers::cost::estimate_usd(price, 1_000),
            0.0
        );

        let Err(err) = build_local_cope_b(None, None) else {
            panic!("an unset policy version must be refused");
        };
        assert!(format!("{err:#}").contains("CHARCOAL_COPE_B_POLICY_VERSION"));

        let model = build_local_cope_b(Some("policy-ddac468-2026-07-03"), None)
            .expect("builds with a policy and the default URL");
        // Requirement (spec D4): local verdicts carry the local identity.
        assert_eq!(
            model.name(),
            crate::toxicity::runpod_cope_b::LOCAL_COPE_B_NAME
        );
        assert_eq!(
            model.model_id(),
            crate::toxicity::runpod_cope_b::LOCAL_COPE_B_MODEL_ID
        );
    }

    #[test]
    fn unknown_models_are_named_in_the_error() {
        let err = match build_eval_model("nope", 1.0) {
            Ok(_) => panic!("unknown model accepted"),
            Err(e) => format!("{e:#}"),
        };
        assert!(err.contains("cope-b") && err.contains("detoxify"), "{err}");
    }

    /// A zero ceiling DISABLES the meter (cost_meter.rs), so the cap must
    /// never round down to it.
    #[test]
    fn the_cap_never_becomes_a_disabled_meter() {
        assert_eq!(cap_cents(1.0), 100);
        assert_eq!(cap_cents(0.004), 1);
        assert_eq!(cap_cents(0.0), 1);
    }
}
