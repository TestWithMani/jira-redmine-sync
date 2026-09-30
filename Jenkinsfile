pipeline {
    agent any
    options {
        disableConcurrentBuilds()
    }
    stages {
        stage('Run sync') {
            steps {
                withCredentials([file(credentialsId: 'jira-redmine-env', variable: 'ENV_FILE')]) {
                    script {
                        if (isUnix()) {
                            sh '''
                                cp "$ENV_FILE" .env
                                chmod 600 .env
                                python3 app.py
                            '''
                        } else {
                            bat '''
                                copy /Y "%ENV_FILE%" .env
                                python app.py
                            '''
                        }
                    }
                }
            }
        }
    }
}
