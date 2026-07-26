# Jenkins

Jenkins uses the generic `labctl` interface. The CLI detects `JENKINS_URL`, `BUILD_ID`, `JOB_NAME`,
`GIT_COMMIT`, and `BUILD_USER_ID` and stores them as provider metadata; the server remains
CI-vendor-neutral.

## Credentials

Create Jenkins Secret Text credentials for the Agent URL and API token. The example uses the IDs
`lab-platform-server` and `lab-platform-token`. Restrict the credentials to the folder/job that
needs hardware access and run the agent on a node that can reach the private lab network.

## Declarative pipeline

[`integrations/jenkins/Jenkinsfile.example`](../integrations/jenkins/Jenkinsfile.example) contains a
starting pipeline. A complete version is:

```groovy
pipeline {
    agent { label 'lab-network' }

    environment {
        LAB_PLATFORM_SERVER = credentials('lab-platform-server')
        LAB_PLATFORM_TOKEN = credentials('lab-platform-token')
    }

    stages {
        stage('Build firmware') {
            steps {
                sh './scripts/build-firmware.sh'
            }
        }

        stage('Hardware test') {
            steps {
                sh '''
                    set +x
                    labctl ci run \
                      --workflow esp32-ci-test \
                      --artifact "firmware=build/firmware.bin" \
                      --input "expected_version=${GIT_COMMIT}" \
                      --require capability=firmware \
                      --require capability=serial \
                      --require capability=reset \
                      --require capability=probe \
                      --label board=esp32 \
                      --allow-simulated \
                      --allow-physical \
                      --wait-timeout 10m \
                      --reservation-duration 30m \
                      --junit-output hardware-results.xml \
                      --artifacts-directory hardware-artifacts
                '''
            }
        }
    }

    post {
        always {
            junit allowEmptyResults: true, testResults: 'hardware-results.xml'
            archiveArtifacts allowEmptyArchive: true,
                artifacts: 'hardware-results.xml,hardware-artifacts/**/*'
        }
    }
}
```

The token remains in `LAB_PLATFORM_TOKEN`; it is never a CLI argument. `set +x` prevents shell
expansion from appearing in logs even if tracing was enabled by a shared wrapper.

## SimLab and physical variants

For required verification, use `--allow-simulated --no-allow-physical`, optionally adding
`--label location=simulation`. For a manually approved physical job, use an `input` stage and:

```groovy
sh '''
    labctl ci run \
      --bench esp32-devkit-01 \
      --workflow esp32-ci-test \
      --artifact "firmware=build/firmware.bin" \
      --input "expected_version=${GIT_COMMIT}" \
      --no-allow-simulated \
      --allow-physical \
      --junit-output hardware-results.xml \
      --artifacts-directory hardware-artifacts
'''
```

## Aborts

A Jenkins abort sends a termination signal to the shell. `labctl` requests cancellation, stops its
heartbeat, waits briefly for cleanup, and finalizes the session. If Jenkins kills the process tree
before that completes, the Agent's missed-heartbeat reaper releases the reservation. Always keep
JUnit and artifact publication in `post { always { ... } }`.
