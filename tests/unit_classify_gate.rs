use charcoal::cli::classify_gate::{evaluate, GateInputs, GateOutcome, GateRow, MIN_SAMPLE};
use charcoal::toxicity::classifier::ClassifierVerdict;

fn v(toxic: bool, conf: f32) -> ClassifierVerdict {
    ClassifierVerdict {
        toxic_token: toxic,
        confidence: conf,
        latency_ms: 1,
        model_id: "m".into(),
        policy_version: "p".into(),
    }
}

// One row pairs a candidate verdict with the reference verdict for the same post.
fn row(id: &str, candidate_toxic: bool, reference_toxic: bool) -> GateRow {
    GateRow {
        id: id.into(),
        candidate: v(candidate_toxic, if candidate_toxic { 0.99 } else { 0.01 }),
        reference: v(reference_toxic, if reference_toxic { 0.99 } else { 0.01 }),
    }
}

#[test]
fn gate_passes_when_agreement_at_least_85pct() {
    // 50 rows, 7 disagreements -> 43/50 = 86% agreement -> passes the 85% bar.
    // (Note: 88% — the live RunPod-vs-Zentropi result — now passes too.)
    let mut rows: Vec<GateRow> = (0..43).map(|i| row(&format!("a{i}"), true, true)).collect();
    rows.extend((0..7).map(|i| row(&format!("d{i}"), true, false)));
    let inputs = GateInputs {
        candidate_name: "runpod".into(),
        reference_name: "zentropi".into(),
        rows,
    };
    assert!(matches!(evaluate(&inputs), GateOutcome::Pass { .. }));
}

#[test]
fn gate_fails_when_agreement_below_85pct() {
    // 50 rows, 8 disagreements -> 42/50 = 84% agreement -> fails.
    let mut rows: Vec<GateRow> = (0..42).map(|i| row(&format!("a{i}"), true, true)).collect();
    rows.extend((0..8).map(|i| row(&format!("d{i}"), true, false)));
    let inputs = GateInputs {
        candidate_name: "runpod".into(),
        reference_name: "zentropi".into(),
        rows,
    };
    match evaluate(&inputs) {
        GateOutcome::Fail { reason, .. } => assert!(reason.contains("agreement below")),
        _ => panic!("expected Fail for sub-85% agreement"),
    }
}

#[test]
fn gate_fails_when_sample_below_min() {
    // Perfect agreement, but only 5 rows -> below MIN_SAMPLE -> fails.
    let rows: Vec<GateRow> = (0..5).map(|i| row(&format!("a{i}"), true, true)).collect();
    assert!(rows.len() < MIN_SAMPLE);
    let inputs = GateInputs {
        candidate_name: "runpod".into(),
        reference_name: "zentropi".into(),
        rows,
    };
    match evaluate(&inputs) {
        GateOutcome::Fail { reason, .. } => assert!(reason.contains("sample too small")),
        _ => panic!("expected Fail for tiny sample"),
    }
}

/// #437 (PR-Agent on #173): a batch whose policy differs from
/// CHARCOAL_COPE_B_POLICY_VERSION is only LOGGED by the RunPod client; its
/// verdicts are still returned. So the gate and compare tools must run each
/// backend's identity probe before classifying, the way the web scan paths
/// and eval-classifiers already do, or a server on the wrong policy (local or
/// RunPod) would be scored as if it were the deployed one.
mod probe_first {
    use async_trait::async_trait;
    use charcoal::toxicity::classifier::{ClassifierVerdict, ToxicityClassifier};
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;

    struct Probed {
        probe_ok: bool,
        classified: AtomicUsize,
    }
    impl Probed {
        fn new(probe_ok: bool) -> Arc<Self> {
            Arc::new(Self {
                probe_ok,
                classified: AtomicUsize::new(0),
            })
        }
    }
    #[async_trait]
    impl ToxicityClassifier for Probed {
        async fn classify(&self, _content: &str) -> anyhow::Result<ClassifierVerdict> {
            self.classified.fetch_add(1, Ordering::SeqCst);
            Ok(ClassifierVerdict {
                toxic_token: false,
                confidence: 0.9,
                latency_ms: 1,
                model_id: "m".into(),
                policy_version: "p".into(),
            })
        }
        async fn probe_identity(&self) -> anyhow::Result<()> {
            if self.probe_ok {
                Ok(())
            } else {
                anyhow::bail!("policy mismatch: endpoint reports policy-other")
            }
        }
        fn name(&self) -> &'static str {
            "probed"
        }
        fn model_id(&self) -> &'static str {
            "m"
        }
        fn policy_version(&self) -> &'static str {
            "p"
        }
        fn threshold(&self) -> f32 {
            0.5
        }
    }

    fn sample() -> tempfile::NamedTempFile {
        let f = tempfile::NamedTempFile::new().unwrap();
        std::fs::write(f.path(), "{\"id\":\"a\",\"content\":\"hello\"}\n").unwrap();
        f
    }

    #[tokio::test]
    async fn gate_refuses_before_classifying_when_a_probe_fails() {
        for (cand_ok, ref_ok) in [(false, true), (true, false)] {
            let cand = Probed::new(cand_ok);
            let refr = Probed::new(ref_ok);
            let out = tempfile::NamedTempFile::new().unwrap();
            let result = charcoal::cli::classify_gate::run(
                cand.clone(),
                refr.clone(),
                sample().path(),
                out.path(),
            )
            .await;
            let Err(err) = result else {
                panic!("a failed probe must stop the gate")
            };
            assert!(format!("{err:#}").contains("policy mismatch"), "{err:#}");
            assert_eq!(cand.classified.load(Ordering::SeqCst), 0);
            assert_eq!(refr.classified.load(Ordering::SeqCst), 0);
        }
    }

    #[tokio::test]
    async fn compare_refuses_before_classifying_when_a_probe_fails() {
        let a = Probed::new(true);
        let b = Probed::new(false);
        let result =
            charcoal::cli::classify_compare::run(a.clone(), b.clone(), sample().path()).await;
        assert!(result.is_err(), "a failed probe must stop the compare");
        assert_eq!(
            a.classified.load(Ordering::SeqCst) + b.classified.load(Ordering::SeqCst),
            0
        );
    }

    #[tokio::test]
    async fn gate_runs_when_both_probes_pass() {
        let cand = Probed::new(true);
        let refr = Probed::new(true);
        let out = tempfile::NamedTempFile::new().unwrap();
        charcoal::cli::classify_gate::run(cand.clone(), refr.clone(), sample().path(), out.path())
            .await
            .expect("gate runs");
        assert_eq!(cand.classified.load(Ordering::SeqCst), 1);
    }
}
