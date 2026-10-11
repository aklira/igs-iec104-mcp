//! The command line of the runtime. Options are parsed by hand: the runtime takes few of them.

use std::fmt;
use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::path::PathBuf;

/// The control address when none is given: loopback, on a port the runtime chooses (§6).
pub const DEFAULT_CONTROL: SocketAddr = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 0);
/// The number of events kept for the control API before the oldest are dropped.
pub const DEFAULT_EVENT_CAPACITY: usize = 1024;
/// The largest number of events the runtime keeps.
const MAX_EVENT_CAPACITY: usize = 1_000_000;
/// The largest common address of a station: 65535 is the global address, which a station refuses.
const MAX_COMMON_ADDRESS: u16 = 65534;

/// What the runtime runs as.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Role {
    /// A client of one station.
    Client {
        /// The address of the station the client connects to.
        connect: SocketAddr,
    },
    /// A station serving the points of a file.
    Server {
        /// The address the station listens on. Port 0 lets the runtime choose a free port and
        /// report it.
        listen: SocketAddr,
        /// The points file (see `points`).
        points: PathBuf,
    },
}

/// What the runtime runs, and how its control API is reached.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Config {
    /// The role: a client or a server.
    pub role: Role,
    /// The common address of the station, or of the station the client talks to: 1 to 65534.
    pub common_address: u16,
    /// The address of the control API. Port 0 lets the runtime choose a free port and report it.
    pub control: SocketAddr,
    /// The number of events kept before the oldest are dropped.
    pub event_capacity: usize,
    /// The file with the control token (see `token`). It is required.
    pub control_token: PathBuf,
}

/// What the command line asks for.
#[derive(Debug, PartialEq, Eq)]
pub enum Invocation {
    /// No arguments: print the identity line and exit.
    Banner,
    /// `--help`: print the usage and exit.
    Usage,
    /// Run the runtime.
    Run(Config),
}

/// A command line that the runtime cannot run.
#[derive(Debug, PartialEq, Eq)]
pub enum ConfigError {
    /// An option the runtime does not know.
    UnknownOption(String),
    /// An option without its value.
    MissingValue(&'static str),
    /// Neither `--connect` nor `--listen` was given.
    MissingRole,
    /// Both `--connect` and `--listen` were given.
    ConflictingRoles,
    /// `--listen` was given without `--points`.
    MissingPoints,
    /// `--points` was given to a client, which serves no points.
    PointsWithClient,
    /// `--control-token-file` was not given: the control API is never open without a token.
    MissingToken,
    /// A value out of its range, or not a valid address or number.
    Invalid {
        /// The option.
        option: &'static str,
        /// The value as given.
        value: String,
    },
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnknownOption(option) => write!(f, "unknown option {option}"),
            Self::MissingValue(option) => write!(f, "{option} needs a value"),
            Self::MissingRole => write!(
                f,
                "give --connect ADDRESS to run a client, or --listen ADDRESS --points FILE to run a station"
            ),
            Self::ConflictingRoles => write!(f, "give either --connect or --listen, not both"),
            Self::MissingPoints => write!(f, "--listen needs --points FILE"),
            Self::PointsWithClient => {
                write!(f, "--points is for a station; a client serves no points")
            }
            Self::MissingToken => write!(f, "--control-token-file FILE is required"),
            Self::Invalid { option, value } => write!(f, "invalid value {value:?} for {option}"),
        }
    }
}

impl std::error::Error for ConfigError {}

/// The usage of the runtime, printed for `--help` and with a command-line error.
pub const USAGE: &str =
    "usage: igs-iec104-runtime (--connect ADDRESS | --listen ADDRESS --points FILE) [options]

  --connect ADDRESS         run a client of the station at ADDRESS, for example 127.0.0.1:2404
  --listen ADDRESS          run a station listening on ADDRESS, for example 127.0.0.1:2404
  --points FILE             the points of the station (JSON, see the runtime documentation)
  --common-address N        the common address of the station, 1 to 65534 (default 1)
  --control ADDRESS         the address of the control API (default 127.0.0.1:0)
  --event-capacity N        the events kept for the control API (default 1024)
  --control-token-file FILE the file with the control token, required
  -h, --help                print this usage

With no arguments, the runtime prints its identity and exits.";

/// Reads a command line, without its program name.
pub fn parse<I>(arguments: I) -> Result<Invocation, ConfigError>
where
    I: IntoIterator<Item = String>,
{
    let mut arguments = arguments.into_iter().peekable();
    if arguments.peek().is_none() {
        return Ok(Invocation::Banner);
    }

    let mut connect = None;
    let mut listen = None;
    let mut points = None;
    let mut common_address = 1;
    let mut control = DEFAULT_CONTROL;
    let mut event_capacity = DEFAULT_EVENT_CAPACITY;
    let mut control_token = None;
    while let Some(argument) = arguments.next() {
        match argument.as_str() {
            "-h" | "--help" => return Ok(Invocation::Usage),
            "--connect" => {
                connect = Some(address("--connect", value("--connect", &mut arguments)?)?);
            }
            "--listen" => {
                listen = Some(address("--listen", value("--listen", &mut arguments)?)?);
            }
            "--points" => points = Some(PathBuf::from(value("--points", &mut arguments)?)),
            "--common-address" => {
                let text = value("--common-address", &mut arguments)?;
                common_address = number("--common-address", &text, 1..=MAX_COMMON_ADDRESS)?;
            }
            "--control" => control = address("--control", value("--control", &mut arguments)?)?,
            "--event-capacity" => {
                let text = value("--event-capacity", &mut arguments)?;
                event_capacity = number("--event-capacity", &text, 1..=MAX_EVENT_CAPACITY)?;
            }
            "--control-token-file" => {
                control_token = Some(PathBuf::from(value(
                    "--control-token-file",
                    &mut arguments,
                )?));
            }
            _ => return Err(ConfigError::UnknownOption(argument)),
        }
    }

    let role = match (connect, listen) {
        (Some(_), Some(_)) => return Err(ConfigError::ConflictingRoles),
        (None, None) => return Err(ConfigError::MissingRole),
        (Some(connect), None) => {
            if points.is_some() {
                return Err(ConfigError::PointsWithClient);
            }
            Role::Client { connect }
        }
        (None, Some(listen)) => Role::Server {
            listen,
            points: points.ok_or(ConfigError::MissingPoints)?,
        },
    };
    let control_token = control_token.ok_or(ConfigError::MissingToken)?;
    Ok(Invocation::Run(Config {
        role,
        common_address,
        control,
        event_capacity,
        control_token,
    }))
}

fn value<I: Iterator<Item = String>>(
    option: &'static str,
    arguments: &mut I,
) -> Result<String, ConfigError> {
    arguments.next().ok_or(ConfigError::MissingValue(option))
}

fn address(option: &'static str, text: String) -> Result<SocketAddr, ConfigError> {
    text.parse().map_err(|_| invalid(option, &text))
}

fn number<T>(
    option: &'static str,
    text: &str,
    range: std::ops::RangeInclusive<T>,
) -> Result<T, ConfigError>
where
    T: std::str::FromStr + PartialOrd + Copy,
{
    let value: T = text.parse().map_err(|_| invalid(option, text))?;
    if range.contains(&value) {
        Ok(value)
    } else {
        Err(invalid(option, text))
    }
}

fn invalid(option: &'static str, value: &str) -> ConfigError {
    ConfigError::Invalid {
        option,
        value: value.to_owned(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(list: &[&str]) -> Vec<String> {
        list.iter().map(|argument| (*argument).to_owned()).collect()
    }

    /// Runs a command line with a token file, as every run needs one.
    fn run(list: &[&str]) -> Result<Config, ConfigError> {
        let mut with_token = list.to_vec();
        with_token.extend(["--control-token-file", "token.txt"]);
        match parse(args(&with_token))? {
            Invocation::Run(config) => Ok(config),
            other => panic!("expected a run, got {other:?}"),
        }
    }

    #[test]
    fn no_arguments_print_the_banner() {
        assert_eq!(parse(Vec::new()), Ok(Invocation::Banner));
    }

    #[test]
    fn help_asks_for_the_usage() {
        assert_eq!(parse(args(&["--help"])), Ok(Invocation::Usage));
        assert_eq!(
            parse(args(&["--connect", "127.0.0.1:2404", "-h"])),
            Ok(Invocation::Usage)
        );
    }

    #[test]
    fn a_run_needs_a_control_token_file() {
        assert_eq!(
            parse(args(&["--connect", "127.0.0.1:2404"])),
            Err(ConfigError::MissingToken)
        );
        let config = run(&["--connect", "127.0.0.1:2404"]).expect("valid");
        assert_eq!(config.control_token, PathBuf::from("token.txt"));
    }

    #[test]
    fn a_client_takes_the_station_to_connect_to() {
        let config = run(&["--connect", "127.0.0.1:2404"]).expect("valid");
        assert_eq!(
            config.role,
            Role::Client {
                connect: "127.0.0.1:2404".parse().expect("address"),
            }
        );
        assert_eq!(config.common_address, 1);
        assert_eq!(config.control, DEFAULT_CONTROL);
        assert_eq!(config.event_capacity, DEFAULT_EVENT_CAPACITY);
    }

    #[test]
    fn a_station_takes_its_listen_address_and_its_points() {
        let config = run(&["--listen", "127.0.0.1:0", "--points", "points.json"]).expect("valid");
        assert_eq!(
            config.role,
            Role::Server {
                listen: "127.0.0.1:0".parse().expect("address"),
                points: PathBuf::from("points.json"),
            }
        );
    }

    #[test]
    fn every_option_is_read() {
        let config = run(&[
            "--connect",
            "10.0.0.2:2404",
            "--common-address",
            "65534",
            "--control",
            "127.0.0.1:9000",
            "--event-capacity",
            "8",
        ])
        .expect("valid");
        assert_eq!(config.common_address, 65534);
        assert_eq!(config.control, "127.0.0.1:9000".parse().expect("address"));
        assert_eq!(config.event_capacity, 8);
    }

    #[test]
    fn exactly_one_role_is_required() {
        assert_eq!(
            parse(args(&["--control", "127.0.0.1:0"])),
            Err(ConfigError::MissingRole)
        );
        assert_eq!(
            parse(args(&[
                "--connect",
                "127.0.0.1:2404",
                "--listen",
                "127.0.0.1:0",
                "--points",
                "p.json"
            ])),
            Err(ConfigError::ConflictingRoles)
        );
    }

    #[test]
    fn a_station_needs_points_and_a_client_takes_none() {
        assert_eq!(
            parse(args(&["--listen", "127.0.0.1:0"])),
            Err(ConfigError::MissingPoints)
        );
        assert_eq!(
            parse(args(&["--connect", "127.0.0.1:2404", "--points", "p.json"])),
            Err(ConfigError::PointsWithClient)
        );
    }

    #[test]
    fn the_common_address_stays_within_the_station_range() {
        for text in ["0", "65535", "70000", "-1", "x"] {
            let result = run(&["--connect", "127.0.0.1:2404", "--common-address", text]);
            assert_eq!(
                result,
                Err(ConfigError::Invalid {
                    option: "--common-address",
                    value: text.to_owned(),
                })
            );
        }
    }

    #[test]
    fn an_invalid_address_is_named_with_its_option() {
        let result = run(&["--connect", "not-an-address"]);
        assert_eq!(
            result,
            Err(ConfigError::Invalid {
                option: "--connect",
                value: "not-an-address".to_owned(),
            })
        );
    }

    #[test]
    fn an_option_without_its_value_is_reported() {
        assert_eq!(
            parse(args(&["--connect"])),
            Err(ConfigError::MissingValue("--connect"))
        );
    }

    #[test]
    fn an_unknown_option_is_refused() {
        assert_eq!(
            parse(args(&["--connect", "127.0.0.1:2404", "--tls"])),
            Err(ConfigError::UnknownOption("--tls".to_owned()))
        );
    }

    #[test]
    fn the_event_capacity_must_be_positive() {
        let result = run(&["--connect", "127.0.0.1:2404", "--event-capacity", "0"]);
        assert!(matches!(
            result,
            Err(ConfigError::Invalid {
                option: "--event-capacity",
                ..
            })
        ));
    }
}
