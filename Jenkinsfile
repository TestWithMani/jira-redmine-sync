pipeline {
    agent any
    options {
        disableConcurrentBuilds()
    }
    triggers {
        // Catch missed webhook creates about every four hours (empty ISSUE_KEY → --reconcile).
        cron('H */4 * * *')
    }
    parameters {
        string(
            name: 'ISSUE_KEY',
            defaultValue: '',
            description: 'Jira issue key for a one-shot sync (for example SUP-1472). Leave empty to reconcile missing tickets.'
        )
    }
    stages {
        stage('Sync') {
            steps {
                withCredentials([file(credentialsId: 'jira-redmine-env', variable: 'ENV_FILE')]) {
                    script {
                        def issueKey = (params.ISSUE_KEY ?: '').trim()
                        if (isUnix()) {
                            if (issueKey) {
                                sh """
                                    cp "\$ENV_FILE" .env
                                    chmod 600 .env
                                    python3 app.py --once '${issueKey}'
                                """
                            } else {
                                sh '''
                                    cp "$ENV_FILE" .env
                                    chmod 600 .env
                                    python3 app.py --reconcile
                                '''
                            }
                        } else {
                            if (issueKey) {
                                bat """
                                    copy /Y "%ENV_FILE%" .env
                                    python app.py --once ${issueKey}
                                """
                            } else {
                                bat '''
                                    copy /Y "%ENV_FILE%" .env
                                    python app.py --reconcile
                                '''
                            }
                        }
                    }
                }
            }
        }
    }
}
