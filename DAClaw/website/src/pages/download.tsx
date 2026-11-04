import React from 'react';
import Link from '@docusaurus/Link';
import Layout from '@theme/Layout';
import styles from './download.module.css';

const VERSION = '0.1.0';
const WINDOWS_RELEASE_URL = `https://github.com/DA-Open/DAClaw/releases/download/v${VERSION}/DAClaw-Setup-${VERSION}.exe`;
const MACOS_RELEASE_URL = `https://github.com/DA-Open/DAClaw/releases/download/v${VERSION}/DAClaw-macOS-${VERSION}.dmg`;

export default function Download(): React.ReactElement {
  return (
    <Layout
      title="Download DAClaw"
      description="Download the Windows installer for the DAClaw local web dashboard."
    >
      <main className={styles.page}>
        <section className={styles.hero}>
          <div className={styles.container}>
            <div className={styles.heroGrid}>
              <div>
                <span className={styles.eyebrow}>Windows installer preview</span>
                <h1 className={styles.title}>Install DAClaw as a local web app.</h1>
                <p className={styles.subtitle}>
                  Download the setup package, choose where DAClaw should live,
                  then launch the local dashboard from a Desktop or Start Menu
                  shortcut. The installer keeps DAClaw dependencies inside its
                  own virtual environment.
                </p>
                <div className={styles.actions}>
                  <Link className={styles.primaryAction} to={WINDOWS_RELEASE_URL}>
                    Download for Windows
                  </Link>
                  <Link className={styles.primaryAction} to={MACOS_RELEASE_URL}>
                    Download for macOS
                  </Link>
                  <Link className={styles.secondaryAction} to="/docs/user-guide/features/web-dashboard">
                    Web dashboard docs
                  </Link>
                </div>
              </div>

              <aside className={styles.panel} aria-label="Installer requirements">
                <h2>Current preview requirements</h2>
                <ul className={styles.requirementList}>
                  <li>
                    <span className={styles.mark}>1</span>
                    <span>Windows 10/11 or macOS 12+.</span>
                  </li>
                  <li>
                    <span className={styles.mark}>2</span>
                    <span>Python 3.11 or newer installed before setup.</span>
                  </li>
                  <li>
                    <span className={styles.mark}>3</span>
                    <span>Internet access during first install so Python dependencies can be resolved.</span>
                  </li>
                </ul>
              </aside>
            </div>
          </div>
        </section>

        <section className={styles.section}>
          <div className={styles.container}>
            <div className={styles.sectionGrid}>
              <article className={styles.infoBlock}>
                <h2>Clean local runtime</h2>
                <p>
                  DAClaw creates a private <code className={styles.code}>venv</code>
                  inside the install folder. It does not install packages into
                  the user's base Python environment.
                </p>
              </article>

              <article className={styles.infoBlock}>
                <h2>No Node or Git needed</h2>
                <p>
                  The installer payload includes the built Web UI. Node.js,
                  npm, and Git are only needed by developers who build a release.
                </p>
              </article>

              <article className={styles.infoBlock}>
                <h2>Uninstall from Windows</h2>
                <p>
                  Remove DAClaw from Windows Settings or the Start Menu. App
                  files and the generated virtual environment are removed;
                  user config under <code className={styles.code}>~/.daclaw</code>
                  is preserved.
                </p>
              </article>
            </div>
          </div>
        </section>
      </main>
    </Layout>
  );
}
