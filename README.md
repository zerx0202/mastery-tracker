# Mastery Tracker

A personal, non-commercial tool for tracking champion mastery progress over time.

## Why

The in-game client shows current mastery points, but not how they change between
sessions. Event pass missions that require mastery gains are tedious to track
manually across a full champion pool. This app records mastery snapshots and
shows the delta.

## Stack

- FastAPI backend, SQLite storage
- Riot API: ACCOUNT-V1, CHAMPION-MASTERY-V4, MATCH-V5, SUMMONER-V4
- Static assets from Data Dragon
- Self-hosted, single user, private network only

## Status

In development.
