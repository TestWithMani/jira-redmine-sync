pipeline {
    agent any
    options {
        disableConcurrentBuilds()
    }
    parameters {
        string(name: 'ISSUE_KEY', defaultValue: '', description: 'Jira issue key, for example SUP-1472')
    }
    stages {
        stage('Sync one issue') {
            steps {
                withCredentials([file(credentialsId: 'jira-redmine-env', variable: 'ENV_FILE')]) {
                    script {
                        if (isUnix()) {
                            sh '''
                                cp "$ENV_FILE" .env
                                chmod 600 .env
                                python3 app.py --once "$ISSUE_KEY"
                            '''
                        } else {
                            bat '''
                                copy /Y "%ENV_FILE%" .env
                                python app.py --once "%ISSUE_KEY%"
                            '''
                        }
                    }
                }
            }
        }
    }
}
