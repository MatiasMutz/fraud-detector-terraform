package writer

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/secretsmanager"

	_ "github.com/lib/pq"
)

const envDBCredentialsSecretARN = "DB_CREDENTIALS_SECRET_ARN"

const awsClientTimeout = 7 * time.Second

type DBConfig struct {
	Host     string
	Port     int
	Name     string
	User     string
	Password string
}

type secretsManagerClient interface {
	GetSecretValue(context.Context, *secretsmanager.GetSecretValueInput, ...func(*secretsmanager.Options)) (*secretsmanager.GetSecretValueOutput, error)
}

type dbCredentials struct {
	Username string `json:"username"`
	Password string `json:"password"`
}

func LoadDBConfigFromEnv(ctx context.Context) (DBConfig, error) {
	awsCfg, err := config.LoadDefaultConfig(ctx, config.WithHTTPClient(&http.Client{Timeout: awsClientTimeout}))
	if err != nil {
		return DBConfig{}, fmt.Errorf("load aws config: %w", err)
	}
	return LoadDBConfigFromEnvWithSecretsManager(ctx, secretsmanager.NewFromConfig(awsCfg))
}

func LoadDBConfigFromEnvWithSecretsManager(ctx context.Context, client secretsManagerClient) (DBConfig, error) {
	port := 5432
	if rawPort := os.Getenv("DB_PORT"); rawPort != "" {
		parsedPort, err := strconv.Atoi(rawPort)
		if err != nil {
			return DBConfig{}, fmt.Errorf("parse DB_PORT: %w", err)
		}
		port = parsedPort
	}

	credentials, err := loadDBCredentials(ctx, client, os.Getenv(envDBCredentialsSecretARN))
	if err != nil {
		return DBConfig{}, err
	}

	return DBConfig{
		Host:     os.Getenv("DB_HOST"),
		Port:     port,
		Name:     valueOrDefault(os.Getenv("DB_NAME"), "fraud_results"),
		User:     credentials.Username,
		Password: credentials.Password,
	}, nil
}

func loadDBCredentials(ctx context.Context, client secretsManagerClient, secretARN string) (dbCredentials, error) {
	if secretARN == "" {
		return dbCredentials{}, fmt.Errorf("%s is not set", envDBCredentialsSecretARN)
	}

	out, err := client.GetSecretValue(ctx, &secretsmanager.GetSecretValueInput{
		SecretId: aws.String(secretARN),
	})
	if err != nil {
		return dbCredentials{}, fmt.Errorf("get db credentials secret: %w", err)
	}
	if out == nil || out.SecretString == nil || *out.SecretString == "" {
		return dbCredentials{}, fmt.Errorf("db credentials secret is missing SecretString")
	}

	var credentials dbCredentials
	if err := json.Unmarshal([]byte(*out.SecretString), &credentials); err != nil {
		return dbCredentials{}, fmt.Errorf("decode db credentials secret: %w", err)
	}
	if credentials.Username == "" || credentials.Password == "" {
		return dbCredentials{}, fmt.Errorf("db credentials secret must contain username and password")
	}

	return credentials, nil
}

func OpenDB(cfg DBConfig) (*sql.DB, error) {
	dsn := fmt.Sprintf(
		"host=%s port=%d dbname=%s user=%s password=%s sslmode=require connect_timeout=5",
		cfg.Host,
		cfg.Port,
		cfg.Name,
		cfg.User,
		cfg.Password,
	)

	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, fmt.Errorf("open postgres: %w", err)
	}

	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)
	db.SetConnMaxLifetime(5 * time.Minute)

	if err := db.Ping(); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("ping postgres: %w", err)
	}

	return db, nil
}

func valueOrDefault(value, fallback string) string {
	if value != "" {
		return value
	}
	return fallback
}
