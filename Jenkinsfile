pipeline {
    agent any

    stages {
        stage('Checkout') {
            steps {
                echo 'CloudOps source checkout is ready.'
            }
        }

        stage('Python Environment') {
            steps {
                bat 'python --version'
            }
        }

        stage('Docker Environment') {
            steps {
                bat 'docker --version'
            }
        }

        stage('Build Docker Image') {
            steps {
                bat 'docker build -t cloudops-platform:latest .'
            }
        }
    }
}