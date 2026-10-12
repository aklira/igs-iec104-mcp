// The station of the independent peer (IMPLEMENTATION.md §13; task X3). It is a small program of
// this project that drives the OpenMUC j60870 library through its public API, as a black box: it
// listens on a port, confirms each interrogation, and answers it with one measured value. It
// keeps no state beyond that value, and it has no protocol logic of its own: the library does the
// IEC 60870-5-104 framing, the link, and the transfer state.
//
// Usage: java -cp classes:j60870.jar Station --port 2404 --common-address 1

import java.io.IOException;
import org.openmuc.j60870.ASdu;
import org.openmuc.j60870.ASduType;
import org.openmuc.j60870.CauseOfTransmission;
import org.openmuc.j60870.Connection;
import org.openmuc.j60870.ConnectionEventListener;
import org.openmuc.j60870.Server;
import org.openmuc.j60870.ServerEventListener;
import org.openmuc.j60870.ie.IeQuality;
import org.openmuc.j60870.ie.InformationObject;
import org.openmuc.j60870.ie.IeShortFloat;
import org.openmuc.j60870.ie.InformationElement;

public final class Station {

    /** The information object address of the point, and its value. */
    static final int POINT_ADDRESS = 100;
    static final float POINT_VALUE = 12.5f;

    private Station() {}

    public static void main(String[] args) throws Exception {
        int port = 2404;
        int commonAddress = 1;
        for (int i = 0; i + 1 < args.length; i += 2) {
            switch (args[i]) {
                case "--port" -> port = Integer.parseInt(args[i + 1]);
                case "--common-address" -> commonAddress = Integer.parseInt(args[i + 1]);
                default -> throw new IllegalArgumentException("unknown option " + args[i]);
            }
        }
        final int address = commonAddress;
        Server server = Server.builder().setPort(port).build();
        server.start(new ServerEventListener() {
            @Override
            public ConnectionEventListener connectionIndication(Connection connection) {
                return listener(address);
            }

            @Override
            public void serverStoppedListeningIndication(IOException e) {
                System.err.println("the station stopped listening: " + e.getMessage());
            }

            @Override
            public void connectionAttemptFailed(IOException e) {
                System.err.println("a connection attempt failed: " + e.getMessage());
            }
        });
        System.out.println("station listening on port " + port + " with common address " + address);
        Thread.currentThread().join();
    }

    private static ConnectionEventListener listener(int commonAddress) {
        return new ConnectionEventListener() {
            @Override
            public void newASdu(Connection connection, ASdu asdu) {
                if (asdu.getTypeIdentification() != ASduType.C_IC_NA_1) {
                    return;
                }
                try {
                    // The activation of the interrogation is confirmed, then the point is sent in
                    // answer to it, then the interrogation is terminated (IEC 60870-5-104 §7.5).
                    connection.sendConfirmation(asdu);
                    InformationElement[][] elements = {
                        {new IeShortFloat(POINT_VALUE), new IeQuality(false, false, false, false, false)}
                    };
                    connection.send(
                            new ASdu(
                                    ASduType.M_ME_NC_1,
                                    false,
                                    CauseOfTransmission.INTERROGATED_BY_STATION,
                                    false,
                                    false,
                                    0,
                                    commonAddress,
                                    new InformationObject(POINT_ADDRESS, elements)));
                    connection.sendActivationTermination(asdu);
                } catch (IOException e) {
                    System.err.println("the answer could not be sent: " + e.getMessage());
                }
            }

            @Override
            public void dataTransferStateChanged(Connection connection, boolean stopped) {
                System.out.println("data transfer " + (stopped ? "stopped" : "started"));
            }

            @Override
            public void connectionClosed(Connection connection, IOException cause) {
                System.out.println("connection closed: " + cause.getMessage());
            }
        };
    }
}
