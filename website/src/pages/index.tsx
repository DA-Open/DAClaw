import React, {useEffect, useRef, useState} from 'react';
import Link from '@docusaurus/Link';
import Layout from '@theme/Layout';
import styles from './index.module.css';

const INSTALL_COMMAND =
  'curl -fsSL https://raw.githubusercontent.com/DA-Open/DAClaw/main/scripts/install.sh | bash';

const ASCII_LOGO = String.raw`██████╗  █████╗  ██████╗██╗      █████╗ ██╗    ██╗       █████╗  ██████╗ ███████╗███╗   ██╗████████╗
██╔══██╗██╔══██╗██╔════╝██║     ██╔══██╗██║    ██║      ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝
██║  ██║███████║██║     ██║     ███████║██║ █╗ ██║█████╗███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║
██║  ██║██╔══██║██║     ██║     ██╔══██║██║███╗██║╚════╝██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║
██████╔╝██║  ██║╚██████╗███████╗██║  ██║╚███╔███╔╝      ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║
╚═════╝ ╚═╝  ╚═╝ ╚═════╝╚══════╝╚═╝  ╚═╝ ╚══╝╚══╝       ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝`;

const terminalLines = [
  '❯ Research the latest approaches to GRPO training and write a summary',
  '  web_search "GRPO reinforcement learning 2026"       1.2s',
  '  web_extract arxiv.org/abs/2402.03300                3.1s',
  '  write_file ~/research/grpo-summary.md               0.1s',
  '',
  "Done. I've written a summary covering:",
  "  ✓ GRPO's group-relative advantage",
  '  ✓ Comparison with PPO/DPO on reasoning benchmarks',
  '  ✓ Implementation notes for Axolotl and TRL',
  '',
  'Saved to ~/research/grpo-summary.md',
];

const features = [
  {
    title: 'Works Across Channels',
    body: 'CLI, Telegram, Discord, Slack, WhatsApp, Signal, email, and webhooks from a single gateway.',
  },
  {
    title: 'Remembers Useful Work',
    body: 'Persistent memory, searchable sessions, and generated skills help the agent improve over time.',
  },
  {
    title: 'Runs Real Tools',
    body: 'Terminal, file operations, browser automation, code execution, cron, delegation, and web research.',
  },
  {
    title: 'Provider Flexible',
    body: 'Use DA-Open ModelHub, AIDP, OpenRouter, OpenAI-compatible endpoints, local models, and more.',
  },
  {
    title: 'Sandbox Aware',
    body: 'Local, Docker, SSH, Modal, Daytona, and Singularity execution environments for different risk levels.',
  },
  {
    title: 'Built For Operators',
    body: 'Profiles, credential pools, fallback providers, skills, MCP, ACP, and gateway automation.',
  },
];

function CopyButton({text}: {text: string}) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    await navigator.clipboard.writeText(text);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  }

  return (
    <button className={styles.copyButton} type="button" onClick={copy}>
      {copied ? 'Copied' : 'Copy'}
    </button>
  );
}

function TerminalDemo() {
  const [visibleLines, setVisibleLines] = useState<string[]>([]);
  const indexRef = useRef(0);

  useEffect(() => {
    const timer = window.setInterval(() => {
      setVisibleLines((current) => {
        const next = terminalLines[indexRef.current];
        indexRef.current += 1;
        if (indexRef.current > terminalLines.length) {
          indexRef.current = 0;
          return [];
        }
        return [...current, next];
      });
    }, 520);

    return () => window.clearInterval(timer);
  }, []);

  return (
    <div className={styles.terminalWindow}>
      <div className={styles.terminalHeader}>
        <span className={styles.dotRed} />
        <span className={styles.dotYellow} />
        <span className={styles.dotGreen} />
        <span className={styles.terminalTitle}>daclaw</span>
      </div>
      <pre className={styles.terminalBody}>
        {visibleLines.join('\n')}
        <span className={styles.cursor}>█</span>
      </pre>
    </div>
  );
}

export default function Home(): React.ReactElement {
  return (
    <Layout
      title="DAClaw Agent"
      description="An open-source agent that grows with you, learns your projects, builds its own skills, and runs anywhere."
    >
      <main className={styles.page}>
        <section className={styles.hero}>
          <div className={styles.heroInner}>
            <div className={styles.badge}>
              <span className={styles.badgeDot} />
              Open Source · MIT License
            </div>

            <pre className={styles.asciiLogo} aria-hidden="true">
              {ASCII_LOGO}
            </pre>

            <h1 className={styles.title}>
              An agent that <span>grows with you.</span>
            </h1>

            <p className={styles.subtitle}>
              DAClaw Agent is an autonomous runtime for real work: terminal
              tasks, browser workflows, code edits, research, memory, skills,
              scheduled jobs, and messaging gateways.
            </p>

            <div className={styles.installBox}>
              <div className={styles.installHeader}>
                <span>Linux / macOS / WSL</span>
                <CopyButton text={INSTALL_COMMAND} />
              </div>
              <code>{INSTALL_COMMAND}</code>
            </div>

            <div className={styles.heroActions}>
              <Link className={styles.primaryAction} to="/docs/getting-started/quickstart">
                Get Started
              </Link>
              <Link className={styles.secondaryAction} to="/docs/reference/cli-commands">
                CLI Reference
              </Link>
            </div>
          </div>
        </section>

        <section className={styles.section}>
          <div className={styles.container}>
            <div className={styles.sectionHeader}>
              <h2>Operator-grade agent runtime</h2>
              <p>
                One install gives you a CLI, tool runtime, memory system,
                skill loader, provider router, and messaging gateway.
              </p>
            </div>

            <div className={styles.featureGrid}>
              {features.map((feature) => (
                <article className={styles.featureCard} key={feature.title}>
                  <h3>{feature.title}</h3>
                  <p>{feature.body}</p>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section className={styles.section}>
          <div className={styles.container}>
            <div className={styles.sectionHeader}>
              <h2>Watch the loop</h2>
              <p>
                DAClaw can search, inspect, edit, run tests, save useful
                patterns, and continue from prior sessions.
              </p>
            </div>
            <TerminalDemo />
          </div>
        </section>

        <section className={styles.installSection}>
          <div className={styles.container}>
            <h2>Start in three commands</h2>
            <div className={styles.steps}>
              <div>
                <span>1</span>
                <code>{INSTALL_COMMAND}</code>
              </div>
              <div>
                <span>2</span>
                <code>daclaw setup</code>
              </div>
              <div>
                <span>3</span>
                <code>daclaw</code>
              </div>
            </div>
            <p>
              Connect DA-Open ModelHub, AIDP, OpenRouter, or any
              OpenAI-compatible endpoint during setup.
            </p>
          </div>
        </section>
      </main>
    </Layout>
  );
}
