#!/usr/bin/env python3
"""
Send test transactions through the on-prem EC2 via SSM.

The EC2 lives in the 192.168.0.0/16 VPC, so its SQS requests pass the
CIDR restriction on the queue policy. AWS credentials come from LabInstanceProfile.

Usage:
    python3 scripts/send_test_transactions.py [options]
    make send-test-tx
    make send-test-tx TX_COUNT=50000 FRAUD_PCT=30 TX_CONCURRENCY=256
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from string import Template
import textwrap
import time

import boto3


def get_instance_id(stack_name: str, region: str) -> str:
    cf = boto3.client("cloudformation", region_name=region)
    resp = cf.describe_stacks(StackName=stack_name)
    for o in resp["Stacks"][0].get("Outputs", []):
        if o["OutputKey"] == "VpnGatewayInstanceId":
            return o["OutputValue"]
    raise RuntimeError(f"VpnGatewayInstanceId not found in stack '{stack_name}'")


def get_queue_url(region: str) -> str:
    result = subprocess.run(
        ["terraform", "output", "-raw", "queue_url"],
        capture_output=True,
        text=True,
    )
    url = result.stdout.strip()
    if result.returncode != 0 or not url:
        raise RuntimeError("Could not read queue_url from terraform output — run 'terraform init' first")
    return url


def build_ec2_script(queue_url: str, region: str, count: int, fraud_pct: int, concurrency: int) -> str:
    # The remote EC2 runs a short-lived Go program so SQS SendMessageBatch calls
    # can be issued concurrently without pulling third-party dependencies.
    template = Template("""
        #!/bin/bash
        set -euo pipefail

        export QUEUE_URL="$queue_url"
        export REGION="$region"
        export COUNT="$count"
        export FRAUD_PCT="$fraud_pct"
        export CONCURRENCY="$concurrency"
        export HOME="$${HOME:-/tmp}"
        export GOCACHE="$${GOCACHE:-/tmp/go-build-cache}"
        mkdir -p "$$GOCACHE"

        if ! command -v go >/dev/null 2>&1; then
            if command -v dnf >/dev/null 2>&1; then
                sudo dnf install -y golang >/dev/null
            else
                sudo yum install -y golang >/dev/null
            fi
        fi

        cat >/tmp/send-test-transactions.go <<'GO'
package main

import (
	"bytes"
	"context"
	cryptorand "crypto/rand"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"math/rand"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

type credentials struct {
	AccessKeyID     string `json:"AccessKeyId"`
	SecretAccessKey string `json:"SecretAccessKey"`
	Token           string `json:"Token"`
}

type batchJob struct {
	start int
	size  int
}

var (
	countries         = []string{"AR", "BR", "CL", "UY", "MX", "CO", "PE", "US"}
	channels          = []string{"web", "mobile", "atm", "pos", "api"}
	normalScenarios   = []string{"trusted_repeat", "returning_daily", "new_user_sparse"}
	fraudScenarios    = []string{"account_drain", "country_shift", "device_shift", "merchant_fanout", "micro_amount_card_testing"}
	currencyByCountry = map[string]string{"AR": "ARS", "BR": "BRL", "CL": "CLP", "UY": "UYU", "MX": "MXN", "CO": "COP", "PE": "PEN", "US": "USD"}
)

func main() {
	queueURL := mustEnv("QUEUE_URL")
	region := mustEnv("REGION")
	count := mustPositiveInt("COUNT")
	fraudPct := clampInt(mustInt("FRAUD_PCT"), 0, 100)
	concurrency := clampInt(mustPositiveInt("CONCURRENCY"), 1, 512)

	creds, err := loadCredentials(context.Background())
	if err != nil {
		fatalf("load credentials: %v", err)
	}

	endpoint, err := sqsEndpoint(queueURL, region)
	if err != nil {
		fatalf("resolve SQS endpoint: %v", err)
	}

	client := &http.Client{
		Timeout: 20 * time.Second,
		Transport: &http.Transport{
			MaxIdleConns:        concurrency * 4,
			MaxIdleConnsPerHost: concurrency * 4,
			IdleConnTimeout:     90 * time.Second,
		},
	}

	started := time.Now()
	jobs := make(chan batchJob, concurrency*4)
	errCh := make(chan error, 1)
	var sent int64
	var fraudSent int64
	var scenarioMu sync.Mutex
	scenarioCounts := map[string]int{}

	var wg sync.WaitGroup
	for workerID := 0; workerID < concurrency; workerID++ {
		wg.Add(1)
		go func(workerID int) {
			defer wg.Done()
			rng := rand.New(rand.NewSource(time.Now().UnixNano() + int64(workerID)*7919))
			for job := range jobs {
				entries := make([]string, 0, job.size)
				localCounts := map[string]int{}
				localFraud := 0
				for i := 0; i < job.size; i++ {
					body, scenario, isFraud := makeTransaction(rng, job.start+i, fraudPct)
					entries = append(entries, body)
					localCounts[scenario]++
					if isFraud {
						localFraud++
					}
				}
				if err := sendBatch(client, endpoint, queueURL, region, creds, entries); err != nil {
					select {
					case errCh <- err:
					default:
					}
					return
				}
				current := atomic.AddInt64(&sent, int64(job.size))
				atomic.AddInt64(&fraudSent, int64(localFraud))
				scenarioMu.Lock()
				for scenario, value := range localCounts {
					scenarioCounts[scenario] += value
				}
				scenarioMu.Unlock()
				if current%5000 == 0 || int(current) == count {
					elapsed := time.Since(started).Seconds()
					fmt.Printf("sent %d/%d batches_concurrency=%d rate=%.0f tx/s\\n", current, count, concurrency, float64(current)/math.Max(elapsed, 0.001))
				}
			}
		}(workerID)
	}

	for index := 1; index <= count; index += 10 {
		size := minInt(10, count-index+1)
		select {
		case err := <-errCh:
			close(jobs)
			wg.Wait()
			fatalf("send failed after %d/%d transaction(s): %v", atomic.LoadInt64(&sent), count, err)
		case jobs <- batchJob{start: index, size: size}:
		}
	}
	close(jobs)
	wg.Wait()
	select {
	case err := <-errCh:
		fatalf("send failed after %d/%d transaction(s): %v", atomic.LoadInt64(&sent), count, err)
	default:
	}

	scenarioPayload, _ := json.Marshal(scenarioCounts)
	elapsed := time.Since(started).Seconds()
	fmt.Printf("Done: %d transaction(s) enqueued in %.2fs at %.0f tx/s; fraud-pattern=%d (%.1f%%).\\n", sent, elapsed, float64(sent)/math.Max(elapsed, 0.001), fraudSent, float64(fraudSent)/math.Max(float64(sent), 1)*100)
	fmt.Printf("Scenario counts: %s\\n", scenarioPayload)
}

func sendBatch(client *http.Client, endpoint, queueURL, region string, creds credentials, bodies []string) error {
	values := url.Values{}
	values.Set("Action", "SendMessageBatch")
	values.Set("Version", "2012-11-05")
	values.Set("QueueUrl", queueURL)
	for i, body := range bodies {
		prefix := fmt.Sprintf("SendMessageBatchRequestEntry.%d.", i+1)
		values.Set(prefix+"Id", fmt.Sprintf("m%d", i))
		values.Set(prefix+"MessageBody", body)
	}
	encoded := values.Encode()
	var lastErr error
	for attempt := 0; attempt < 5; attempt++ {
		req, err := http.NewRequest(http.MethodPost, endpoint, strings.NewReader(encoded))
		if err != nil {
			return err
		}
		req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
		signRequest(req, region, creds, encoded)
		resp, err := client.Do(req)
		if err == nil {
			bodyBytes, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			if resp.StatusCode >= 200 && resp.StatusCode < 300 && !bytes.Contains(bodyBytes, []byte("<BatchResultErrorEntry>")) {
				return nil
			}
			lastErr = fmt.Errorf("sqs status=%d body=%s", resp.StatusCode, string(bodyBytes))
			if resp.StatusCode < 500 && resp.StatusCode != 429 {
				return lastErr
			}
		} else {
			lastErr = err
		}
		time.Sleep(time.Duration(100*(attempt+1)*(attempt+1)) * time.Millisecond)
	}
	return lastErr
}

func signRequest(req *http.Request, region string, creds credentials, payload string) {
	now := time.Now().UTC()
	amzDate := now.Format("20060102T150405Z")
	dateStamp := now.Format("20060102")
	payloadHash := sha256Hex([]byte(payload))

	headers := map[string]string{
		"content-type": "application/x-www-form-urlencoded",
		"host":         req.URL.Host,
		"x-amz-date":   amzDate,
	}
	if creds.Token != "" {
		headers["x-amz-security-token"] = creds.Token
		req.Header.Set("X-Amz-Security-Token", creds.Token)
	}
	names := make([]string, 0, len(headers))
	for name := range headers {
		names = append(names, name)
	}
	sort.Strings(names)

	var canonicalHeaders strings.Builder
	for _, name := range names {
		canonicalHeaders.WriteString(name)
		canonicalHeaders.WriteString(":")
		canonicalHeaders.WriteString(headers[name])
		canonicalHeaders.WriteString("\\n")
	}
	signedHeaders := strings.Join(names, ";")
	canonicalRequest := strings.Join([]string{
		http.MethodPost,
		"/",
		"",
		canonicalHeaders.String(),
		signedHeaders,
		payloadHash,
	}, "\\n")

	scope := dateStamp + "/" + region + "/sqs/aws4_request"
	stringToSign := "AWS4-HMAC-SHA256\\n" + amzDate + "\\n" + scope + "\\n" + sha256Hex([]byte(canonicalRequest))
	signingKey := hmacSHA256(hmacSHA256(hmacSHA256(hmacSHA256([]byte("AWS4"+creds.SecretAccessKey), dateStamp), region), "sqs"), "aws4_request")
	signature := hex.EncodeToString(hmacSHA256(signingKey, stringToSign))
	req.Header.Set("Authorization", "AWS4-HMAC-SHA256 Credential="+creds.AccessKeyID+"/"+scope+", SignedHeaders="+signedHeaders+", Signature="+signature)
	req.Header.Set("X-Amz-Date", amzDate)
}

func makeTransaction(rng *rand.Rand, index, fraudPct int) (string, string, bool) {
	isFraud := rng.Intn(100) < fraudPct
	scenario := pick(rng, normalScenarios)
	if isFraud {
		scenario = pick(rng, fraudScenarios)
	}
	userID := chooseUser(rng)
	homeCountry := userHomeCountry(userID)
	country := homeCountry
	if scenario == "country_shift" || scenario == "account_drain" {
		country = alternateCountry(rng, homeCountry)
	}
	currency := currencyByCountry[country]
	if isFraud && rng.Float64() < 0.35 {
		currency = "USD"
	}
	channel := pick(rng, channels)
	if isFraud {
		channel = pick(rng, []string{"web", "mobile", "api"})
	}

	var amount, oldOrg float64
	if isFraud && scenario == "micro_amount_card_testing" {
		amount = round(rngFloat(rng, 1, 35), 2)
		oldOrg = round(rngFloat(rng, 100, 5000), 2)
	} else if isFraud {
		oldOrg = round(rngFloat(rng, 50000, 250000), 2)
		amount = oldOrg
		if scenario != "account_drain" {
			amount = round(rngFloat(rng, 2500, math.Min(180000, oldOrg)), 2)
		}
	} else {
		oldOrg = round(rngFloat(rng, 1000, 80000), 2)
		amount = round(rngFloat(rng, 25, math.Max(50, math.Min(oldOrg*0.45, 3500))), 2)
	}

	oldDest := round(rngFloat(rng, 0, 40000), 2)
	if isFraud && (scenario == "account_drain" || scenario == "merchant_fanout") {
		oldDest = 0
	}
	destPrefix := "merchant"
	if isFraud {
		destPrefix = "acc"
	}

	message := map[string]interface{}{
		"trace_id":            randomTraceID(),
		"transaction_id":      fmt.Sprintf("%d-%d", time.Now().UnixNano(), index),
		"user_id":             userID,
		"amount":              amount,
		"currency":            currency,
		"timestamp":           time.Now().UTC().Add(-time.Duration(rng.Intn(7*24*3600)) * time.Second).Format(time.RFC3339),
		"channel":             channel,
		"destination_account": fmt.Sprintf("%s-%06d", destPrefix, rng.Intn(999999)+1),
		"country":             country,
		"oldbalance_org":      oldOrg,
		"newbalance_orig":     math.Max(0, round(oldOrg-amount, 2)),
		"oldbalance_dest":     oldDest,
		"newbalance_dest":     round(oldDest+amount, 2),
		"features":            featureBlob(rng, isFraud, amount),
	}
	payload, _ := json.Marshal(message)
	return string(payload), scenario, isFraud
}

func randomTraceID() string {
	var b [16]byte
	if _, err := cryptorand.Read(b[:]); err != nil {
		return fmt.Sprintf("trace-%d", time.Now().UnixNano())
	}
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

func featureBlob(rng *rand.Rand, isFraud bool, amount float64) map[string]float64 {
	base := map[string]float64{"amount": amount, "amount_log1p": round(math.Log1p(amount), 6)}
	var vMin, vMax float64
	if isFraud {
		priorMean := rngFloat(rng, 20, 900)
		destUnique5 := rng.Intn(3) + 3
		base["card1"], base["card2"], base["card3"], base["card5"] = float64(rng.Intn(601)+1500), float64(rng.Intn(101)+120), float64(rng.Intn(51)+140), float64(rng.Intn(31)+210)
		base["addr1"], base["addr2"], base["dist1"], base["dist2"] = float64(rng.Intn(121)+260), float64(rng.Intn(21)+55), rngFloat(rng, 4, 18), rngFloat(rng, 3, 16)
		base["m1"], base["m2"], base["m3"], base["m5"], base["m6"], base["m7"], base["m8"], base["m9"] = 0, 0, 0, float64(rng.Intn(2)), 0, 0, 0, 0
		base["c1"], base["c2"], base["c4"], base["c7"], base["c10"], base["c14"] = rngFloat(rng, 3, 7), rngFloat(rng, 3.5, 8), rngFloat(rng, 2, 5.5), rngFloat(rng, 2, 6), rngFloat(rng, 2, 5), rngFloat(rng, 2.5, 6.5)
		base["d1"], base["d2"], base["d3"], base["d4"], base["d5"] = rngFloat(rng, 0.001, 0.08), rngFloat(rng, 12, 70), rngFloat(rng, 2, 8), rngFloat(rng, 2, 9), rngFloat(rng, 3, 12)
		base["id_01"], base["id_02"], base["id_05"], base["id_11"], base["id_17"], base["id_23"], base["id_30"], base["id_33"], base["id_38"] = rngFloat(rng, -9, -4.5), rngFloat(rng, 20, 95), rngFloat(rng, 0.1, 1.6), rngFloat(rng, 0.1, 1.2), rngFloat(rng, 6, 12), rngFloat(rng, 7, 14), rngFloat(rng, 3, 7), rngFloat(rng, 0.01, 1), rngFloat(rng, 2.5, 5.5)
		base["previous_transaction_amount"] = rngFloat(rng, 5, math.Max(50, priorMean))
		base["prior_5_transaction_count"], base["prior_10_transaction_count"] = float64(rng.Intn(4)+2), float64(rng.Intn(6)+5)
		base["prior_5_amount_sum"], base["prior_5_amount_mean"], base["prior_5_amount_std"] = rngFloat(rng, priorMean*2, priorMean*5), priorMean, rngFloat(rng, 5, math.Max(8, priorMean*0.35))
		base["prior_10_amount_sum"], base["prior_10_amount_mean"], base["prior_10_amount_std"] = rngFloat(rng, priorMean*5, priorMean*10), rngFloat(rng, priorMean*0.8, priorMean*1.2), rngFloat(rng, 8, math.Max(12, priorMean*0.45))
		base["seconds_since_previous_transaction"] = pickFloat(rng, []float64{rngFloat(rng, 5, 90), rngFloat(rng, 90, 900), rngFloat(rng, 900, 3600)})
		base["prior_5_unique_name_dest_count"], base["prior_10_unique_name_dest_count"] = float64(destUnique5), float64(destUnique5+rng.Intn(11-destUnique5))
		vMin, vMax = 3, 5.5
	} else {
		priorMean := math.Max(25, amount*rngFloat(rng, 0.75, 1.3))
		base["card1"], base["card2"], base["card3"], base["card5"] = float64(rng.Intn(601)+900), float64(rng.Intn(81)+90), float64(rng.Intn(31)+150), float64(rng.Intn(16)+215)
		base["addr1"], base["addr2"], base["dist1"], base["dist2"] = float64(rng.Intn(81)+180), float64(rng.Intn(16)+50), rngFloat(rng, 0, 2), rngFloat(rng, 0, 2)
		base["m1"], base["m2"], base["m3"], base["m5"], base["m6"], base["m7"], base["m8"], base["m9"] = 1, 1, 1, 1, 1, 1, 1, 1
		base["c1"], base["c2"], base["c4"], base["c7"], base["c10"], base["c14"] = rngFloat(rng, 0.4, 2), rngFloat(rng, 0.4, 2), rngFloat(rng, 0, 0.9), rngFloat(rng, 0.2, 1.8), rngFloat(rng, 0.2, 1.5), rngFloat(rng, 0.1, 1.2)
		base["d1"], base["d2"], base["d3"], base["d4"], base["d5"] = rngFloat(rng, 2, 18), rngFloat(rng, 0.1, 3), rngFloat(rng, 0.1, 2), rngFloat(rng, 0.1, 2), rngFloat(rng, 0.1, 1.5)
		base["id_01"], base["id_02"], base["id_05"], base["id_11"], base["id_17"], base["id_23"], base["id_30"], base["id_33"], base["id_38"] = rngFloat(rng, -3.5, -0.2), rngFloat(rng, 90, 180), rngFloat(rng, 1.5, 5), rngFloat(rng, 1.2, 4), rngFloat(rng, 0.1, 2), rngFloat(rng, 0.1, 1.5), rngFloat(rng, 0.3, 2), rngFloat(rng, 8, 24), rngFloat(rng, 0.2, 1.5)
		base["previous_transaction_amount"] = rngFloat(rng, priorMean*0.6, priorMean*1.4)
		base["prior_5_transaction_count"], base["prior_10_transaction_count"] = float64(rng.Intn(5)+1), float64(rng.Intn(8)+3)
		base["prior_5_amount_sum"], base["prior_5_amount_mean"], base["prior_5_amount_std"] = rngFloat(rng, priorMean*2, priorMean*5), priorMean, rngFloat(rng, 2, math.Max(4, priorMean*0.25))
		base["prior_10_amount_sum"], base["prior_10_amount_mean"], base["prior_10_amount_std"] = rngFloat(rng, priorMean*5, priorMean*10), rngFloat(rng, priorMean*0.85, priorMean*1.15), rngFloat(rng, 4, math.Max(6, priorMean*0.30))
		base["seconds_since_previous_transaction"] = pickFloat(rng, []float64{rngFloat(rng, 3600, 21600), rngFloat(rng, 21600, 86400), rngFloat(rng, 86400, 604800)})
		base["prior_5_unique_name_dest_count"], base["prior_10_unique_name_dest_count"] = float64(rng.Intn(2)+1), float64(rng.Intn(3)+1)
		vMin, vMax = 0.05, 0.9
	}
	for _, name := range []string{"v1", "v20", "v61", "v81", "v101", "v130", "v181", "v201", "v241", "v280", "v307", "v320"} {
		base[name] = rngFloat(rng, vMin, vMax)
	}
	for key, value := range base {
		base[key] = round(value, 3)
	}
	return base
}

func loadCredentials(ctx context.Context) (credentials, error) {
	if os.Getenv("AWS_ACCESS_KEY_ID") != "" && os.Getenv("AWS_SECRET_ACCESS_KEY") != "" {
		return credentials{AccessKeyID: os.Getenv("AWS_ACCESS_KEY_ID"), SecretAccessKey: os.Getenv("AWS_SECRET_ACCESS_KEY"), Token: os.Getenv("AWS_SESSION_TOKEN")}, nil
	}
	client := &http.Client{Timeout: 2 * time.Second}
	tokenReq, _ := http.NewRequestWithContext(ctx, http.MethodPut, "http://169.254.169.254/latest/api/token", nil)
	tokenReq.Header.Set("X-aws-ec2-metadata-token-ttl-seconds", "21600")
	tokenResp, err := client.Do(tokenReq)
	if err != nil {
		return credentials{}, err
	}
	tokenBytes, _ := io.ReadAll(tokenResp.Body)
	tokenResp.Body.Close()
	token := string(tokenBytes)
	roleReq, _ := http.NewRequestWithContext(ctx, http.MethodGet, "http://169.254.169.254/latest/meta-data/iam/security-credentials/", nil)
	roleReq.Header.Set("X-aws-ec2-metadata-token", token)
	roleResp, err := client.Do(roleReq)
	if err != nil {
		return credentials{}, err
	}
	roleBytes, _ := io.ReadAll(roleResp.Body)
	roleResp.Body.Close()
	role := strings.TrimSpace(string(roleBytes))
	if role == "" {
		return credentials{}, errors.New("empty IMDS role name")
	}
	credReq, _ := http.NewRequestWithContext(ctx, http.MethodGet, "http://169.254.169.254/latest/meta-data/iam/security-credentials/"+role, nil)
	credReq.Header.Set("X-aws-ec2-metadata-token", token)
	credResp, err := client.Do(credReq)
	if err != nil {
		return credentials{}, err
	}
	defer credResp.Body.Close()
	var creds credentials
	if err := json.NewDecoder(credResp.Body).Decode(&creds); err != nil {
		return credentials{}, err
	}
	if creds.AccessKeyID == "" || creds.SecretAccessKey == "" {
		return credentials{}, errors.New("incomplete IMDS credentials")
	}
	return creds, nil
}

func sqsEndpoint(queueURL, region string) (string, error) {
	parsed, err := url.Parse(queueURL)
	if err != nil {
		return "", err
	}
	host := parsed.Host
	if host == "" {
		host = "sqs." + region + ".amazonaws.com"
	}
	return "https://" + host + "/", nil
}

func mustEnv(name string) string {
	value := os.Getenv(name)
	if value == "" {
		fatalf("%s is required", name)
	}
	return value
}

func mustPositiveInt(name string) int {
	value := mustInt(name)
	if value <= 0 {
		fatalf("%s must be a positive integer", name)
	}
	return value
}

func mustInt(name string) int {
	value, err := strconv.Atoi(mustEnv(name))
	if err != nil {
		fatalf("%s must be an integer", name)
	}
	return value
}

func fatalf(format string, args ...interface{}) {
	fmt.Fprintf(os.Stderr, format+"\\n", args...)
	os.Exit(1)
}

func chooseUser(rng *rand.Rand) string {
	if rng.Float64() < 0.85 {
		return fmt.Sprintf("u-%04d", rng.Intn(2000)+1)
	}
	return fmt.Sprintf("new-%d-%d", time.Now().UnixNano(), rng.Intn(100000))
}

func userHomeCountry(userID string) string {
	sum := 0
	for _, char := range userID {
		sum += int(char)
	}
	return countries[sum%len(countries)]
}

func alternateCountry(rng *rand.Rand, home string) string {
	for {
		country := pick(rng, countries)
		if country != home {
			return country
		}
	}
}

func pick(rng *rand.Rand, values []string) string { return values[rng.Intn(len(values))] }
func pickFloat(rng *rand.Rand, values []float64) float64 { return values[rng.Intn(len(values))] }
func rngFloat(rng *rand.Rand, min, max float64) float64 { return min + rng.Float64()*(max-min) }
func round(value float64, digits int) float64 {
	scale := math.Pow10(digits)
	return math.Round(value*scale) / scale
}
func minInt(a, b int) int { if a < b { return a }; return b }
func clampInt(value, min, max int) int {
	if value < min { return min }
	if value > max { return max }
	return value
}
func sha256Hex(payload []byte) string {
	sum := sha256.Sum256(payload)
	return hex.EncodeToString(sum[:])
}
func hmacSHA256(key []byte, data string) []byte {
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte(data))
	return mac.Sum(nil)
}
GO

        go run /tmp/send-test-transactions.go
    """)
    return textwrap.dedent(template.substitute(
        queue_url=queue_url,
        region=region,
        count=count,
        fraud_pct=fraud_pct,
        concurrency=concurrency,
    )).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stack",       default="itba-tp-fraud-onprem-strongswan", help="CloudFormation stack name for the on-prem EC2")
    parser.add_argument("--region",      default="us-east-1")
    parser.add_argument("--count",      type=int, default=50000,  help="Number of transactions to send")
    parser.add_argument("--fraud-pct", type=int, default=20, help="Percentage of transactions with fraud pattern (0-100)")
    parser.add_argument("--concurrency", type=int, default=128, help="Concurrent SQS batch send workers on the on-prem EC2")
    parser.add_argument("--queue-url",   default="", help="Override SQS queue URL (default: read from terraform output)")
    parser.add_argument("--instance-id", default="", help="Override EC2 instance ID (default: read from CloudFormation)")
    args = parser.parse_args()

    region      = args.region
    instance_id = args.instance_id or get_instance_id(args.stack, region)
    queue_url   = args.queue_url   or get_queue_url(region)

    print(f"Instance  : {instance_id}")
    print(f"Queue     : {queue_url}")
    print(f"Count     : {args.count}")
    print(f"Fraud pct : {args.fraud_pct}%\n")
    print(f"Concurrency: {args.concurrency}\n")

    ec2_script = build_ec2_script(queue_url, region, args.count, args.fraud_pct, args.concurrency)
    command = f"bash << 'BEOF'\n{ec2_script}\nBEOF"

    ssm    = boto3.client("ssm", region_name=region)
    resp   = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [command]},
        Comment=f"send-test-tx count={args.count} fraud_pct={args.fraud_pct} concurrency={args.concurrency}",
        CloudWatchOutputConfig={
            "CloudWatchLogGroupName": "/ssm/itba-tp-fraud/send-test-transactions",
            "CloudWatchOutputEnabled": True,
        },
    )
    cmd_id = resp["Command"]["CommandId"]
    print(f"SSM command ID: {cmd_id}")
    print("Waiting", end="", flush=True)

    max_wait_seconds = max(900, min(7200, args.count // 10 + 600))
    for _ in range(max_wait_seconds // 3):
        time.sleep(3)
        inv    = ssm.get_command_invocation(CommandId=cmd_id, InstanceId=instance_id)
        status = inv["Status"]
        print(".", end="", flush=True)
        if status in ("Success", "Failed", "Cancelled", "TimedOut"):
            print(f" {status}\n")
            stdout = inv.get("StandardOutputContent", "").strip()
            stderr = inv.get("StandardErrorContent", "").strip()
            if stdout:
                print(stdout)
            if stderr:
                print("STDERR:", stderr, file=sys.stderr)
            sys.exit(0 if status == "Success" else 1)

    print(" timed out waiting for SSM command")
    sys.exit(1)


if __name__ == "__main__":
    main()
