C=======================================================================
C     HYDRAU   -   MULTIPHASE WELLBORE HYDRAULICS (BEGGS AND BRILL)
C
C     WRITTEN   22-FEB-1990   D. LEBLANC   HOUSTON
C     REVISED   16-OCT-1992   PAYNE ET AL HOLDUP CORRECTION ADDED
C     REVISED   03-MAR-1995   ANNULAR FLOW FOR CEMENT DISPLACEMENT
C
C     DEPENDS ON PVTCOR:  PVTSET IS CALLED ONCE PER SEGMENT AND THE
C     PROPERTIES ARE PICKED UP OUT OF /PVTOUT/.  DO NOT CALL ANY
C     ROUTINE HERE BEFORE PVTINI.
C
C     FLOW REGIME CODES RETURNED IN IREG:
C         1 SEGREGATED   2 TRANSITION   3 INTERMITTENT   4 DISTRIBUTED
C=======================================================================
      SUBROUTINE BBDPDZ (P, QO, QW, QG, DIA, THETA, EPS,
     &                   DPDZ, HL, IREG)
C-----------------------------------------------------------------------
C     PRESSURE GRADIENT, PSI/FT, FOR ONE PIPE SEGMENT.
C         P     PRESSURE AT SEGMENT, PSIA
C         QO    OIL RATE,   STB/D
C         QW    WATER RATE, STB/D
C         QG    GAS RATE,   MSCF/D
C         DIA   INTERNAL DIAMETER, FT
C         THETA INCLINATION FROM HORIZONTAL, DEGREES
C         EPS   ABSOLUTE ROUGHNESS, FT
C-----------------------------------------------------------------------
C     --- native2py: expanded INCLUDE 'PETRO.INC' ---
C=======================================================================
C     PETRO.INC   -   GLOBAL FLUID PROPERTY COMMON BLOCKS
C
C     ORIGINALLY WRITTEN  14-MAR-1988   R.T. HALSEY    DUNCAN, OK
C     REVISED             02-SEP-1991   ADDED VAZQUEZ-BEGGS RS
C     REVISED             11-JUN-1994   RAISED NCMAX FROM 12 TO 20
C     REVISED             30-JAN-1997   Y2K - NONE REQUIRED IN THIS DECK
C
C     *** DO NOT REORDER THE COMMON BLOCKS.  THE BLANK COMMON IS
C     *** EQUIVALENCED AGAINST THE SCRATCH ARRAY IN SIMCOR AND ANY
C     *** CHANGE HERE WILL SILENTLY CORRUPT THE IMPES SOLVE.
C=======================================================================
      IMPLICIT DOUBLE PRECISION (A-H,O-Z)
      IMPLICIT INTEGER (I-N)
C
      PARAMETER (NCMAX  = 20)
      PARAMETER (NPMAX  = 3)
      PARAMETER (NTABMX = 50)
      PARAMETER (ZERO   = 0.0D0)
      PARAMETER (ONE    = 1.0D0)
      PARAMETER (HALF   = 0.5D0)
      PARAMETER (SMALL  = 1.0D-12)
      PARAMETER (PATM   = 14.6959D0)
      PARAMETER (TABS   = 459.67D0)
C
C-----FLUID SPECIFICATION -----------------------------------------------
C     NCOMP  - NUMBER OF HYDROCARBON COMPONENTS IN USE
C     API    - STOCK TANK OIL GRAVITY, DEG API
C     SGG    - GAS SPECIFIC GRAVITY (AIR = 1.0)
C     SGW    - BRINE SPECIFIC GRAVITY
C     TRES   - RESERVOIR TEMPERATURE, DEG F
C     PB     - BUBBLE POINT PRESSURE, PSIA (COMPUTED BY PVTBUB)
C
      COMMON /FLUID/ API, SGG, SGW, TRES, PB, PSEP, TSEP, NCOMP
C
C-----PER COMPONENT DATA ------------------------------------------------
      COMMON /COMPS/ ZFEED(NCMAX), PCRIT(NCMAX), TCRIT(NCMAX),
     &               ACENT(NCMAX), WTMOL(NCMAX), BIC(NCMAX,NCMAX)
C
C-----LAST COMPUTED PVT STATE (SET BY PVTSET, READ BY EVERYTHING) --------
C     THESE ARE DELIBERATELY GLOBAL.  THE 1988 CALLING CONVENTION WAS
C     "CALL PVTSET(P) THEN USE THE COMMON".  DO NOT ADD ARGUMENTS.
C
      COMMON /PVTOUT/ BO, BG, BW, RSOL, VISO, VISG, VISW,
     &                RHOO, RHOG, RHOW, ZFAC, CO, CG, CW
C
C-----ERROR / DIAGNOSTIC STATE -----------------------------------------
C     IERR   - 0 = OK, >0 = FATAL, <0 = WARNING (SEE PVTERR)
C     LUNPRT - PRINT UNIT (6 = STDOUT, 7 = .PRT FILE)
C
      COMMON /DIAG/ IERR, IWARN, NITPVT, LUNPRT, LUNDBG, IDBGLV
C
C     --- native2py: end INCLUDE 'PETRO.INC' ---
      DOUBLE PRECISION NFR, NLV, NRE
      DATA GC /32.174D0/, PI /3.14159265358979D0/
C
      CALL PVTSET (P)
      IF (IERR .NE. 0) THEN
         DPDZ = ZERO
         HL   = ONE
         IREG = 0
         RETURN
      END IF
C
      AREA = 0.25D0*PI*DIA*DIA
      IF (AREA .LT. SMALL .OR. DIA .LT. SMALL) THEN
         IERR = 31
         RETURN
      END IF
C
C-----IN SITU VOLUMETRIC RATES, FT3/SEC ---------------------------------
      QOI = QO * BO * 5.615D0 / 86400.0D0
      QWI = QW * BW * 5.615D0 / 86400.0D0
      QGF = QG*1000.0D0 - QO*RSOL
      IF (QGF .LT. ZERO) QGF = ZERO
      QGI = QGF * BG / 86400.0D0
      QL  = QOI + QWI
      QT  = QL  + QGI
      IF (QT .LT. SMALL) THEN
         DPDZ = ZERO
         HL   = ONE
         IREG = 3
         RETURN
      END IF
C
      VSL = QL  / AREA
      VSG = QGI / AREA
      VM  = QT  / AREA
      CL  = QL  / QT
C
C-----MIXTURE PROPERTIES ------------------------------------------------
      IF (QL .GT. SMALL) THEN
         FO   = QOI / QL
         RHOL = FO*RHOO + (ONE-FO)*RHOW
         VISL = FO*VISO + (ONE-FO)*VISW
         SIGL = FO*30.0D0 + (ONE-FO)*70.0D0
      ELSE
         RHOL = RHOO
         VISL = VISO
         SIGL = 30.0D0
      END IF
C
      NFR = VM*VM / (GC*DIA)
      NLV = 1.938D0 * VSL * (RHOL/SIGL)**0.25D0
C
C-----FLOW REGIME BOUNDARIES -------------------------------------------
      IF (CL .LT. SMALL) CL = SMALL
      L1 = 0
      A1 = 316.0D0  * CL**0.302D0
      A2 = 0.0009252D0 * CL**(-2.4684D0)
      A3 = 0.10D0   * CL**(-1.4516D0)
      A4 = 0.50D0   * CL**(-6.738D0)
C
      IF (CL .LT. 0.01D0 .AND. NFR .LT. A1) THEN
         IREG = 1
      ELSE IF (CL .GE. 0.01D0 .AND. NFR .LT. A2) THEN
         IREG = 1
      ELSE IF (CL .GE. 0.01D0 .AND. NFR .GE. A2 .AND. NFR .LE. A3) THEN
         IREG = 2
      ELSE IF (CL .LT. 0.4D0 .AND. NFR .GE. A3 .AND. NFR .LE. A1) THEN
         IREG = 3
      ELSE IF (CL .GE. 0.4D0 .AND. NFR .GT. A3 .AND. NFR .LE. A4) THEN
         IREG = 3
      ELSE
         IREG = 4
      END IF
C
C-----HORIZONTAL HOLDUP -------------------------------------------------
      IF (IREG .EQ. 2) THEN
         DEN = A3 - A2
         IF (DABS(DEN) .LT. SMALL) THEN
            WT = HALF
         ELSE
            WT = (A3 - NFR) / DEN
         END IF
         H1 = HLHOR(CL, NFR, 1)
         H3 = HLHOR(CL, NFR, 3)
         HL0 = WT*H1 + (ONE-WT)*H3
      ELSE
         HL0 = HLHOR(CL, NFR, IREG)
      END IF
      IF (HL0 .LT. CL) HL0 = CL
C
C-----INCLINATION CORRECTION -------------------------------------------
      ANG = THETA * PI / 180.0D0
      PSI = ONE + BBCFAC(CL, NLV, NFR, IREG, THETA)
     &            * (DSIN(1.8D0*ANG) - 0.333D0*DSIN(1.8D0*ANG)**3)
      HL  = HL0 * PSI
C     ---- 16-OCT-1992 PAYNE CORRECTION.  UPHILL ONLY.
      IF (THETA .GT. ZERO) HL = 0.924D0 * HL
      IF (HL .GT. ONE ) HL = ONE
      IF (HL .LT. CL  ) HL = CL
C
C-----FRICTION ----------------------------------------------------------
      RHOS = RHOL*HL + RHOG*(ONE-HL)
      RHON = RHOL*CL + RHOG*(ONE-CL)
      VISN = VISL*CL + VISG*(ONE-CL)
      IF (VISN .LT. SMALL) VISN = SMALL
      NRE  = 1488.0D0 * RHON * VM * DIA / VISN
      FN   = FANNIN(NRE, EPS/DIA)
C
      Y = CL / (HL*HL)
      IF (Y .GT. 1.0D0 .AND. Y .LT. 1.2D0) THEN
         S = DLOG(2.2D0*Y - 1.2D0)
      ELSE IF (Y .GT. SMALL) THEN
         AL = DLOG(Y)
         S  = AL / (-0.0523D0 + 3.182D0*AL - 0.8725D0*AL*AL
     &              + 0.01853D0*AL**4)
      ELSE
         S = ZERO
      END IF
      FTP = FN * DEXP(S)
C
      DPEL = RHOS * DSIN(ANG) / 144.0D0
      DPFR = 2.0D0 * FTP * RHON * VM*VM / (GC * DIA * 144.0D0)
      EK   = RHOS * VM * VSG / (GC * P * 144.0D0)
      IF (EK .GE. ONE) EK = 0.99D0
      DPDZ = (DPEL + DPFR) / (ONE - EK)
      RETURN
      END
C
C-----------------------------------------------------------------------
      DOUBLE PRECISION FUNCTION HLHOR (CL, NFR, IREG)
C-----------------------------------------------------------------------
C     --- native2py: expanded INCLUDE 'PETRO.INC' ---
C=======================================================================
C     PETRO.INC   -   GLOBAL FLUID PROPERTY COMMON BLOCKS
C
C     ORIGINALLY WRITTEN  14-MAR-1988   R.T. HALSEY    DUNCAN, OK
C     REVISED             02-SEP-1991   ADDED VAZQUEZ-BEGGS RS
C     REVISED             11-JUN-1994   RAISED NCMAX FROM 12 TO 20
C     REVISED             30-JAN-1997   Y2K - NONE REQUIRED IN THIS DECK
C
C     *** DO NOT REORDER THE COMMON BLOCKS.  THE BLANK COMMON IS
C     *** EQUIVALENCED AGAINST THE SCRATCH ARRAY IN SIMCOR AND ANY
C     *** CHANGE HERE WILL SILENTLY CORRUPT THE IMPES SOLVE.
C=======================================================================
      IMPLICIT DOUBLE PRECISION (A-H,O-Z)
      IMPLICIT INTEGER (I-N)
C
      PARAMETER (NCMAX  = 20)
      PARAMETER (NPMAX  = 3)
      PARAMETER (NTABMX = 50)
      PARAMETER (ZERO   = 0.0D0)
      PARAMETER (ONE    = 1.0D0)
      PARAMETER (HALF   = 0.5D0)
      PARAMETER (SMALL  = 1.0D-12)
      PARAMETER (PATM   = 14.6959D0)
      PARAMETER (TABS   = 459.67D0)
C
C-----FLUID SPECIFICATION -----------------------------------------------
C     NCOMP  - NUMBER OF HYDROCARBON COMPONENTS IN USE
C     API    - STOCK TANK OIL GRAVITY, DEG API
C     SGG    - GAS SPECIFIC GRAVITY (AIR = 1.0)
C     SGW    - BRINE SPECIFIC GRAVITY
C     TRES   - RESERVOIR TEMPERATURE, DEG F
C     PB     - BUBBLE POINT PRESSURE, PSIA (COMPUTED BY PVTBUB)
C
      COMMON /FLUID/ API, SGG, SGW, TRES, PB, PSEP, TSEP, NCOMP
C
C-----PER COMPONENT DATA ------------------------------------------------
      COMMON /COMPS/ ZFEED(NCMAX), PCRIT(NCMAX), TCRIT(NCMAX),
     &               ACENT(NCMAX), WTMOL(NCMAX), BIC(NCMAX,NCMAX)
C
C-----LAST COMPUTED PVT STATE (SET BY PVTSET, READ BY EVERYTHING) --------
C     THESE ARE DELIBERATELY GLOBAL.  THE 1988 CALLING CONVENTION WAS
C     "CALL PVTSET(P) THEN USE THE COMMON".  DO NOT ADD ARGUMENTS.
C
      COMMON /PVTOUT/ BO, BG, BW, RSOL, VISO, VISG, VISW,
     &                RHOO, RHOG, RHOW, ZFAC, CO, CG, CW
C
C-----ERROR / DIAGNOSTIC STATE -----------------------------------------
C     IERR   - 0 = OK, >0 = FATAL, <0 = WARNING (SEE PVTERR)
C     LUNPRT - PRINT UNIT (6 = STDOUT, 7 = .PRT FILE)
C
      COMMON /DIAG/ IERR, IWARN, NITPVT, LUNPRT, LUNDBG, IDBGLV
C
C     --- native2py: end INCLUDE 'PETRO.INC' ---
      DOUBLE PRECISION NFR
      DIMENSION AA(4), BB(4), CC(4)
      DATA AA /0.98D0, 0.845D0, 0.845D0, 1.065D0/
      DATA BB /0.4846D0, 0.5351D0, 0.5351D0, 0.5824D0/
      DATA CC /0.0868D0, 0.0173D0, 0.0173D0, 0.0609D0/
C
      IR = IREG
      IF (IR .LT. 1 .OR. IR .GT. 4) IR = 3
      IF (NFR .LT. SMALL) THEN
         HLHOR = CL
      ELSE
         HLHOR = AA(IR) * CL**BB(IR) / NFR**CC(IR)
      END IF
      IF (HLHOR .GT. ONE) HLHOR = ONE
      RETURN
      END
C
C-----------------------------------------------------------------------
      DOUBLE PRECISION FUNCTION BBCFAC (CL, NLV, NFR, IREG, THETA)
C-----------------------------------------------------------------------
C     THE C COEFFICIENT IN THE BEGGS-BRILL INCLINATION CORRECTION.
C-----------------------------------------------------------------------
C     --- native2py: expanded INCLUDE 'PETRO.INC' ---
C=======================================================================
C     PETRO.INC   -   GLOBAL FLUID PROPERTY COMMON BLOCKS
C
C     ORIGINALLY WRITTEN  14-MAR-1988   R.T. HALSEY    DUNCAN, OK
C     REVISED             02-SEP-1991   ADDED VAZQUEZ-BEGGS RS
C     REVISED             11-JUN-1994   RAISED NCMAX FROM 12 TO 20
C     REVISED             30-JAN-1997   Y2K - NONE REQUIRED IN THIS DECK
C
C     *** DO NOT REORDER THE COMMON BLOCKS.  THE BLANK COMMON IS
C     *** EQUIVALENCED AGAINST THE SCRATCH ARRAY IN SIMCOR AND ANY
C     *** CHANGE HERE WILL SILENTLY CORRUPT THE IMPES SOLVE.
C=======================================================================
      IMPLICIT DOUBLE PRECISION (A-H,O-Z)
      IMPLICIT INTEGER (I-N)
C
      PARAMETER (NCMAX  = 20)
      PARAMETER (NPMAX  = 3)
      PARAMETER (NTABMX = 50)
      PARAMETER (ZERO   = 0.0D0)
      PARAMETER (ONE    = 1.0D0)
      PARAMETER (HALF   = 0.5D0)
      PARAMETER (SMALL  = 1.0D-12)
      PARAMETER (PATM   = 14.6959D0)
      PARAMETER (TABS   = 459.67D0)
C
C-----FLUID SPECIFICATION -----------------------------------------------
C     NCOMP  - NUMBER OF HYDROCARBON COMPONENTS IN USE
C     API    - STOCK TANK OIL GRAVITY, DEG API
C     SGG    - GAS SPECIFIC GRAVITY (AIR = 1.0)
C     SGW    - BRINE SPECIFIC GRAVITY
C     TRES   - RESERVOIR TEMPERATURE, DEG F
C     PB     - BUBBLE POINT PRESSURE, PSIA (COMPUTED BY PVTBUB)
C
      COMMON /FLUID/ API, SGG, SGW, TRES, PB, PSEP, TSEP, NCOMP
C
C-----PER COMPONENT DATA ------------------------------------------------
      COMMON /COMPS/ ZFEED(NCMAX), PCRIT(NCMAX), TCRIT(NCMAX),
     &               ACENT(NCMAX), WTMOL(NCMAX), BIC(NCMAX,NCMAX)
C
C-----LAST COMPUTED PVT STATE (SET BY PVTSET, READ BY EVERYTHING) --------
C     THESE ARE DELIBERATELY GLOBAL.  THE 1988 CALLING CONVENTION WAS
C     "CALL PVTSET(P) THEN USE THE COMMON".  DO NOT ADD ARGUMENTS.
C
      COMMON /PVTOUT/ BO, BG, BW, RSOL, VISO, VISG, VISW,
     &                RHOO, RHOG, RHOW, ZFAC, CO, CG, CW
C
C-----ERROR / DIAGNOSTIC STATE -----------------------------------------
C     IERR   - 0 = OK, >0 = FATAL, <0 = WARNING (SEE PVTERR)
C     LUNPRT - PRINT UNIT (6 = STDOUT, 7 = .PRT FILE)
C
      COMMON /DIAG/ IERR, IWARN, NITPVT, LUNPRT, LUNDBG, IDBGLV
C
C     --- native2py: end INCLUDE 'PETRO.INC' ---
      DOUBLE PRECISION NLV, NFR
C
      IF (THETA .GE. ZERO) THEN
         IF (IREG .EQ. 1) THEN
            D = 0.011D0
            E = -3.768D0
            F = 3.539D0
            G = -1.614D0
         ELSE IF (IREG .EQ. 3) THEN
            D = 2.96D0
            E = 0.305D0
            F = -0.4473D0
            G = 0.0978D0
         ELSE
            BBCFAC = ZERO
            RETURN
         END IF
      ELSE
         D = 4.70D0
         E = -0.3692D0
         F = 0.1244D0
         G = -0.5056D0
      END IF
C
      X = D * CL**E * NLV**F * NFR**G
      IF (X .LE. SMALL) THEN
         BBCFAC = ZERO
      ELSE
         BBCFAC = (ONE - CL) * DLOG(X)
      END IF
      IF (BBCFAC .LT. ZERO) BBCFAC = ZERO
      RETURN
      END
C
C-----------------------------------------------------------------------
      DOUBLE PRECISION FUNCTION FANNIN (RE, RELR)
C-----------------------------------------------------------------------
C     FANNING FRICTION FACTOR.  LAMINAR BELOW 2000, COLEBROOK ABOVE,
C     SOLVED BY THREE FIXED POINT SWEEPS FROM THE JAIN EXPLICIT FORM.
C-----------------------------------------------------------------------
C     --- native2py: expanded INCLUDE 'PETRO.INC' ---
C=======================================================================
C     PETRO.INC   -   GLOBAL FLUID PROPERTY COMMON BLOCKS
C
C     ORIGINALLY WRITTEN  14-MAR-1988   R.T. HALSEY    DUNCAN, OK
C     REVISED             02-SEP-1991   ADDED VAZQUEZ-BEGGS RS
C     REVISED             11-JUN-1994   RAISED NCMAX FROM 12 TO 20
C     REVISED             30-JAN-1997   Y2K - NONE REQUIRED IN THIS DECK
C
C     *** DO NOT REORDER THE COMMON BLOCKS.  THE BLANK COMMON IS
C     *** EQUIVALENCED AGAINST THE SCRATCH ARRAY IN SIMCOR AND ANY
C     *** CHANGE HERE WILL SILENTLY CORRUPT THE IMPES SOLVE.
C=======================================================================
      IMPLICIT DOUBLE PRECISION (A-H,O-Z)
      IMPLICIT INTEGER (I-N)
C
      PARAMETER (NCMAX  = 20)
      PARAMETER (NPMAX  = 3)
      PARAMETER (NTABMX = 50)
      PARAMETER (ZERO   = 0.0D0)
      PARAMETER (ONE    = 1.0D0)
      PARAMETER (HALF   = 0.5D0)
      PARAMETER (SMALL  = 1.0D-12)
      PARAMETER (PATM   = 14.6959D0)
      PARAMETER (TABS   = 459.67D0)
C
C-----FLUID SPECIFICATION -----------------------------------------------
C     NCOMP  - NUMBER OF HYDROCARBON COMPONENTS IN USE
C     API    - STOCK TANK OIL GRAVITY, DEG API
C     SGG    - GAS SPECIFIC GRAVITY (AIR = 1.0)
C     SGW    - BRINE SPECIFIC GRAVITY
C     TRES   - RESERVOIR TEMPERATURE, DEG F
C     PB     - BUBBLE POINT PRESSURE, PSIA (COMPUTED BY PVTBUB)
C
      COMMON /FLUID/ API, SGG, SGW, TRES, PB, PSEP, TSEP, NCOMP
C
C-----PER COMPONENT DATA ------------------------------------------------
      COMMON /COMPS/ ZFEED(NCMAX), PCRIT(NCMAX), TCRIT(NCMAX),
     &               ACENT(NCMAX), WTMOL(NCMAX), BIC(NCMAX,NCMAX)
C
C-----LAST COMPUTED PVT STATE (SET BY PVTSET, READ BY EVERYTHING) --------
C     THESE ARE DELIBERATELY GLOBAL.  THE 1988 CALLING CONVENTION WAS
C     "CALL PVTSET(P) THEN USE THE COMMON".  DO NOT ADD ARGUMENTS.
C
      COMMON /PVTOUT/ BO, BG, BW, RSOL, VISO, VISG, VISW,
     &                RHOO, RHOG, RHOW, ZFAC, CO, CG, CW
C
C-----ERROR / DIAGNOSTIC STATE -----------------------------------------
C     IERR   - 0 = OK, >0 = FATAL, <0 = WARNING (SEE PVTERR)
C     LUNPRT - PRINT UNIT (6 = STDOUT, 7 = .PRT FILE)
C
      COMMON /DIAG/ IERR, IWARN, NITPVT, LUNPRT, LUNDBG, IDBGLV
C
C     --- native2py: end INCLUDE 'PETRO.INC' ---
      DOUBLE PRECISION RE
C
      IF (RE .LT. SMALL) THEN
         FANNIN = ZERO
         RETURN
      END IF
      IF (RE .LT. 2000.0D0) THEN
         FANNIN = 16.0D0 / RE
         RETURN
      END IF
C
      FI = -1.8D0 * DLOG10(6.9D0/RE + (RELR/3.7D0)**1.11D0)
      DO 100 IT = 1, 3
         IF (DABS(FI) .LT. SMALL) GO TO 110
         FI = -2.0D0 * DLOG10(RELR/3.7D0 + 2.51D0/(RE*FI))
  100 CONTINUE
  110 IF (DABS(FI) .LT. SMALL) THEN
         FANNIN = 0.005D0
      ELSE
         FANNIN = 0.25D0 / (FI*FI)
      END IF
      RETURN
      END
C
C-----------------------------------------------------------------------
      SUBROUTINE TRAVER (PWH, QO, QW, QG, DIA, EPS, TVD, MD, NSEG, PBH)
C-----------------------------------------------------------------------
C     MARCH A PRESSURE TRAVERSE DOWN THE TUBING FROM WELLHEAD PWH TO
C     BOTTOMHOLE PBH IN NSEG SEGMENTS.  ITERATES ON SEGMENT MIDPOINT
C     PRESSURE BECAUSE THE PVT IS PRESSURE DEPENDENT.
C-----------------------------------------------------------------------
C     --- native2py: expanded INCLUDE 'PETRO.INC' ---
C=======================================================================
C     PETRO.INC   -   GLOBAL FLUID PROPERTY COMMON BLOCKS
C
C     ORIGINALLY WRITTEN  14-MAR-1988   R.T. HALSEY    DUNCAN, OK
C     REVISED             02-SEP-1991   ADDED VAZQUEZ-BEGGS RS
C     REVISED             11-JUN-1994   RAISED NCMAX FROM 12 TO 20
C     REVISED             30-JAN-1997   Y2K - NONE REQUIRED IN THIS DECK
C
C     *** DO NOT REORDER THE COMMON BLOCKS.  THE BLANK COMMON IS
C     *** EQUIVALENCED AGAINST THE SCRATCH ARRAY IN SIMCOR AND ANY
C     *** CHANGE HERE WILL SILENTLY CORRUPT THE IMPES SOLVE.
C=======================================================================
      IMPLICIT DOUBLE PRECISION (A-H,O-Z)
      IMPLICIT INTEGER (I-N)
C
      PARAMETER (NCMAX  = 20)
      PARAMETER (NPMAX  = 3)
      PARAMETER (NTABMX = 50)
      PARAMETER (ZERO   = 0.0D0)
      PARAMETER (ONE    = 1.0D0)
      PARAMETER (HALF   = 0.5D0)
      PARAMETER (SMALL  = 1.0D-12)
      PARAMETER (PATM   = 14.6959D0)
      PARAMETER (TABS   = 459.67D0)
C
C-----FLUID SPECIFICATION -----------------------------------------------
C     NCOMP  - NUMBER OF HYDROCARBON COMPONENTS IN USE
C     API    - STOCK TANK OIL GRAVITY, DEG API
C     SGG    - GAS SPECIFIC GRAVITY (AIR = 1.0)
C     SGW    - BRINE SPECIFIC GRAVITY
C     TRES   - RESERVOIR TEMPERATURE, DEG F
C     PB     - BUBBLE POINT PRESSURE, PSIA (COMPUTED BY PVTBUB)
C
      COMMON /FLUID/ API, SGG, SGW, TRES, PB, PSEP, TSEP, NCOMP
C
C-----PER COMPONENT DATA ------------------------------------------------
      COMMON /COMPS/ ZFEED(NCMAX), PCRIT(NCMAX), TCRIT(NCMAX),
     &               ACENT(NCMAX), WTMOL(NCMAX), BIC(NCMAX,NCMAX)
C
C-----LAST COMPUTED PVT STATE (SET BY PVTSET, READ BY EVERYTHING) --------
C     THESE ARE DELIBERATELY GLOBAL.  THE 1988 CALLING CONVENTION WAS
C     "CALL PVTSET(P) THEN USE THE COMMON".  DO NOT ADD ARGUMENTS.
C
      COMMON /PVTOUT/ BO, BG, BW, RSOL, VISO, VISG, VISW,
     &                RHOO, RHOG, RHOW, ZFAC, CO, CG, CW
C
C-----ERROR / DIAGNOSTIC STATE -----------------------------------------
C     IERR   - 0 = OK, >0 = FATAL, <0 = WARNING (SEE PVTERR)
C     LUNPRT - PRINT UNIT (6 = STDOUT, 7 = .PRT FILE)
C
      COMMON /DIAG/ IERR, IWARN, NITPVT, LUNPRT, LUNDBG, IDBGLV
C
C     --- native2py: end INCLUDE 'PETRO.INC' ---
      DOUBLE PRECISION MD
C
      IF (NSEG .LT. 1) NSEG = 20
      DL  = MD  / DBLE(NSEG)
      DZ  = TVD / DBLE(NSEG)
      IF (DL .LT. SMALL) THEN
         PBH = PWH
         RETURN
      END IF
      SINA = DZ / DL
      IF (SINA .GT. ONE ) SINA =  ONE
      IF (SINA .LT. -ONE) SINA = -ONE
      THETA = DASIN(SINA) * 180.0D0 / 3.14159265358979D0
C
      P = PWH
      DO 200 IS = 1, NSEG
         PM = P
         DO 150 IT = 1, 10
            CALL BBDPDZ (PM, QO, QW, QG, DIA, THETA, EPS,
     &                   DPDZ, HL, IREG)
            IF (IERR .NE. 0) RETURN
            PNEW = P + DPDZ*DL
            PMN  = HALF*(P + PNEW)
            IF (DABS(PMN - PM) .LT. 1.0D-4*MAX(ONE,PM)) GO TO 160
            PM = PMN
  150    CONTINUE
         IWARN = IWARN + 1
  160    P = P + DPDZ*DL
         IF (P .LT. PATM) P = PATM
  200 CONTINUE
      PBH = P
      RETURN
      END