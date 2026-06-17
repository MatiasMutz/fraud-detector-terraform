package writer

import (
	"context"
	"errors"
	"testing"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/secretsmanager"
)

type fakeSecretsManager struct {
	output *secretsmanager.GetSecretValueOutput
	err    error
	calls  []string
}

func (f *fakeSecretsManager) GetSecretValue(_ context.Context, input *secretsmanager.GetSecretValueInput, _ ...func(*secretsmanager.Options)) (*secretsmanager.GetSecretValueOutput, error) {
	f.calls = append(f.calls, aws.ToString(input.SecretId))
	if f.err != nil {
		return nil, f.err
	}
	return f.output, nil
}

func TestLoadDBConfigFromEnvWithSecretsManager(t *testing.T) {
	t.Setenv("DB_HOST", "db.example")
	t.Setenv("DB_PORT", "5433")
	t.Setenv("DB_NAME", "fraud_results")
	t.Setenv(envDBCredentialsSecretARN, "arn:aws:secretsmanager:us-east-1:123456789012:secret:db")

	client := &fakeSecretsManager{
		output: &secretsmanager.GetSecretValueOutput{
			SecretString: aws.String(`{"username":"fraud_admin","password":"secret"}`),
		},
	}

	cfg, err := LoadDBConfigFromEnvWithSecretsManager(context.Background(), client)
	if err != nil {
		t.Fatalf("LoadDBConfigFromEnvWithSecretsManager() error = %v", err)
	}

	if got, want := cfg.Host, "db.example"; got != want {
		t.Fatalf("Host = %q, want %q", got, want)
	}
	if got, want := cfg.Port, 5433; got != want {
		t.Fatalf("Port = %d, want %d", got, want)
	}
	if got, want := cfg.Name, "fraud_results"; got != want {
		t.Fatalf("Name = %q, want %q", got, want)
	}
	if got, want := cfg.User, "fraud_admin"; got != want {
		t.Fatalf("User = %q, want %q", got, want)
	}
	if got, want := cfg.Password, "secret"; got != want {
		t.Fatalf("Password = %q, want %q", got, want)
	}
	if got, want := client.calls, []string{"arn:aws:secretsmanager:us-east-1:123456789012:secret:db"}; len(got) != len(want) || got[0] != want[0] {
		t.Fatalf("secret calls = %#v, want %#v", got, want)
	}
}

func TestLoadDBConfigRequiresSecretARN(t *testing.T) {
	client := &fakeSecretsManager{}

	_, err := LoadDBConfigFromEnvWithSecretsManager(context.Background(), client)
	if err == nil {
		t.Fatal("LoadDBConfigFromEnvWithSecretsManager() error = nil, want error")
	}
	if len(client.calls) != 0 {
		t.Fatalf("secret calls = %#v, want none", client.calls)
	}
}

func TestLoadDBConfigWrapsSecretsManagerError(t *testing.T) {
	t.Setenv(envDBCredentialsSecretARN, "arn:aws:secretsmanager:us-east-1:123456789012:secret:db")
	client := &fakeSecretsManager{err: errors.New("access denied")}

	_, err := LoadDBConfigFromEnvWithSecretsManager(context.Background(), client)
	if err == nil {
		t.Fatal("LoadDBConfigFromEnvWithSecretsManager() error = nil, want error")
	}
}

func TestLoadDBConfigRejectsIncompleteSecret(t *testing.T) {
	t.Setenv(envDBCredentialsSecretARN, "arn:aws:secretsmanager:us-east-1:123456789012:secret:db")
	client := &fakeSecretsManager{
		output: &secretsmanager.GetSecretValueOutput{
			SecretString: aws.String(`{"username":"fraud_admin"}`),
		},
	}

	_, err := LoadDBConfigFromEnvWithSecretsManager(context.Background(), client)
	if err == nil {
		t.Fatal("LoadDBConfigFromEnvWithSecretsManager() error = nil, want error")
	}
}

func TestLoadDBConfigRejectsEmptySecretString(t *testing.T) {
	t.Setenv(envDBCredentialsSecretARN, "arn:aws:secretsmanager:us-east-1:123456789012:secret:db")
	client := &fakeSecretsManager{output: &secretsmanager.GetSecretValueOutput{}}

	_, err := LoadDBConfigFromEnvWithSecretsManager(context.Background(), client)
	if err == nil {
		t.Fatal("LoadDBConfigFromEnvWithSecretsManager() error = nil, want error")
	}
}

func TestLoadDBConfigRejectsInvalidSecretJSON(t *testing.T) {
	t.Setenv(envDBCredentialsSecretARN, "arn:aws:secretsmanager:us-east-1:123456789012:secret:db")
	client := &fakeSecretsManager{
		output: &secretsmanager.GetSecretValueOutput{
			SecretString: aws.String(`{not-json`),
		},
	}

	_, err := LoadDBConfigFromEnvWithSecretsManager(context.Background(), client)
	if err == nil {
		t.Fatal("LoadDBConfigFromEnvWithSecretsManager() error = nil, want error")
	}
}
