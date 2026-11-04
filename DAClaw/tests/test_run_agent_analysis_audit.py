from run_agent import AIAgent


def test_should_run_analysis_audit_with_data_analyst_prompt():
    agent = AIAgent.__new__(AIAgent)
    agent._cached_system_prompt = "You are operating in data-analyst mode."
    assert agent._should_run_analysis_audit("hello", []) is True


def test_build_analysis_audit_repair_prompt_mentions_findings():
    agent = AIAgent.__new__(AIAgent)
    prompt = agent._build_analysis_audit_repair_prompt(
        {
            "summary": "numbers drifted",
            "recommended_action": "revise_report",
            "issues": [
                {
                    "severity": "high",
                    "stage": "final_report",
                    "type": "numeric_mismatch",
                    "message": "Value mismatch",
                    "evidence": "step 8 says 11.8%",
                    "suggested_action": "revise_report",
                }
            ],
        }
    )
    assert "numbers drifted" in prompt
    assert "numeric_mismatch" in prompt
